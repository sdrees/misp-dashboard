import math, random
import os
import json
import datetime, time
import json
import redis
from collections import OrderedDict

import geoip2.database
import phonenumbers, pycountry
from phonenumbers import geocoder

import util

class Geo_helper:
    def __init__(self, serv_redis_db, cfg):
        self.serv_redis_db = serv_redis_db
        self.cfg = cfg
        self.serv_coord = redis.StrictRedis(
                host=cfg.get('RedisGlobal', 'host'),
                port=cfg.getint('RedisGlobal', 'port'),
                db=cfg.getint('RedisMap', 'db'))

        self.keyCategCoord = "GEO_COORD"
        self.keyCategCountry = "GEO_COUNTRY"
        self.keyCategRad = "GEO_RAD"
        self.PATH_TO_DB = cfg.get('RedisMap', 'pathMaxMindDB')
        self.PATH_TO_JSON = cfg.get('RedisMap', 'path_countrycode_to_coord_JSON')
        self.CHANNELDISP = cfg.get('RedisMap', 'channelDisp')

        self.reader = geoip2.database.Reader(self.PATH_TO_DB)
        self.country_to_iso = { country.name: country.alpha_2 for country in pycountry.countries}
        with open(self.PATH_TO_JSON) as f:
            self.country_code_to_coord = json.load(f)

    ''' GET '''
    def getTopCoord(self, date):
            topNum = 6 # default Num
            data = util.getZrange(self.serv_redis_db, self.keyCategCoord, date, topNum)
            return data

    def getHitMap(self, date):
        topNum = 0 # all
        data = util.getZrange(self.serv_redis_db, self.keyCategCountry, date, topNum)
        return data

    def getCoordsByRadius(self, dateStart, dateEnd, centerLat, centerLon, radius):
        dico_coord = {}
        to_return = []
        delta = dateEnd - dateStart
        for i in range(delta.days+1):
            correctDatetime = dateStart + datetime.timedelta(days=i)
            date_str = util.getDateStrFormat(correctDatetime)
            keyname = "{}:{}".format(self.keyCategRad, date_str)
            res = self.serv_redis_db.georadius(keyname, centerLon, centerLat, radius, unit='km', withcoord=True)

            #sum up really close coord
            for data, coord in res:
                flag_added = False
                coord = [coord[0], coord[1]]
                #list all coord
                for dicoCoordStr in dico_coord.keys():
                    dicoCoord = json.loads(dicoCoordStr)
                    #if curCoord close to coord
                    if self.isCloseTo(dicoCoord, coord):
                        #add data to dico coord
                        dico_coord[dicoCoordStr].append(data)
                        flag_added = True
                        break
                # coord not in dic
                if not flag_added:
                    dico_coord[str(coord)] = [data]

            for dicoCoord, array in dico_coord.items():
                dicoCoord = json.loads(dicoCoord)
                to_return.append([array, dicoCoord])
        return to_return

    ''' ADD '''
    def getCoordFromIpAndPublish(self, supposed_ip, categ):
        try:
            rep = self.ip_to_coord(supposed_ip)
            coord = rep['coord']
            coord_dic = {'lat': coord['lat'], 'lon': coord['lon']}
            ordDic = OrderedDict() #keep fields with the same layout in redis
            ordDic['lat'] = coord_dic['lat']
            ordDic['lon'] = coord_dic['lon']
            coord_list = [coord['lat'], coord['lon']]
            self.push_to_redis_zset(self.keyCategCoord, json.dumps(ordDic))
            self.push_to_redis_zset(self.keyCategCountry, rep['full_rep'].country.iso_code)
            ordDic = OrderedDict() #keep fields with the same layout in redis
            ordDic['categ'] = categ
            ordDic['value'] = supposed_ip
            self.push_to_redis_geo(self.keyCategRad, coord['lon'], coord['lat'], json.dumps(ordDic))
            to_send = {
                    "coord": coord,
                    "categ": categ,
                    "value": supposed_ip,
                    "country": rep['full_rep'].country.name,
                    "specifName": rep['full_rep'].subdivisions.most_specific.name,
                    "cityName": rep['full_rep'].city.name,
                    "regionCode": rep['full_rep'].country.iso_code,
                    }
            self.serv_coord.publish(self.CHANNELDISP, json.dumps(to_send))
        except ValueError:
            print("can't resolve ip")
        except geoip2.errors.AddressNotFoundError:
            print("Address not in Database")

    def getCoordFromPhoneAndPublish(self, phoneNumber, categ):
        try:
            rep = phonenumbers.parse(phoneNumber, None)
            if not (phonenumbers.is_valid_number(rep) or phonenumbers.is_possible_number(rep)):
                print("Phone number not valid")
                return
            country_name = geocoder.country_name_for_number(rep, "en")
            country_code = self.country_to_iso[country_name]
            if country_code is None:
                print("Non matching ISO_CODE")
                return
            coord = self.country_code_to_coord[country_code.lower()]  # countrycode is in upper case
            coord_dic = {'lat': coord['lat'], 'lon': coord['long']}

            ordDic = OrderedDict() #keep fields with the same layout in redis
            ordDic['lat'] = coord_dic['lat']
            ordDic['lon'] = coord_dic['lon']
            coord_list = [coord['lat'], coord['long']]
            self.push_to_redis_zset(self.keyCategCoord, json.dumps(ordDic))
            self.push_to_redis_zset(self.keyCategCountry, country_code)
            ordDic = OrderedDict() #keep fields with the same layout in redis
            ordDic['categ'] = categ
            ordDic['value'] = phoneNumber
            self.push_to_redis_geo(self.keyCategRad, coord['long'], coord['lat'], json.dumps(ordDic))
            to_send = {
                    "coord": coord_dic,
                    "categ": categ,
                    "value": phoneNumber,
                    "country": country_name,
                    "specifName": "",
                    "cityName": "",
                    "regionCode": country_code,
                    }
            self.serv_coord.publish(self.CHANNELDISP, json.dumps(to_send))
        except phonenumbers.NumberParseException:
            print("Can't resolve phone number country")

    ''' UTIL '''
    def push_to_redis_geo(self, keyCateg, lon, lat, content):
        now = datetime.datetime.now()
        today_str = util.getDateStrFormat(now)
        keyname = "{}:{}".format(keyCateg, today_str)
        self.serv_redis_db.geoadd(keyname, lon, lat, content)
    def push_to_redis_zset(self, keyCateg, toAdd, endSubkey="", count=1):
        now = datetime.datetime.now()
        today_str = util.getDateStrFormat(now)
        keyname = "{}:{}{}".format(keyCateg, today_str, endSubkey)
        self.serv_redis_db.zincrby(keyname, toAdd, count)


    def ip_to_coord(self, ip):
        resp = self.reader.city(ip)
        lat = float(resp.location.latitude)
        lon = float(resp.location.longitude)
        # 0.0001 correspond to ~10m
        # Cast the float so that it has the correct float format
        lat_corrected = float("{:.4f}".format(lat))
        lon_corrected = float("{:.4f}".format(lon))
        return { 'coord': {'lat': lat_corrected, 'lon': lon_corrected}, 'full_rep': resp }

    def isCloseTo(self, coord1, coord2):
        clusterMeter = self.cfg.getfloat('GEO' ,'clusteringDistance')
        clusterThres = math.pow(10, len(str(abs(clusterMeter)))-7) #map meter to coord threshold (~ big approx)
        if abs(float(coord1[0]) - float(coord2[0])) <= clusterThres:
            if abs(float(coord1[1]) - float(coord2[1])) <= clusterThres:
                return True
        return False
