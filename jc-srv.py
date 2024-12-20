'''
SPDX-License-Identifier: MPL-2.0
SPDX-FileCopyrightText: 2023 Martin Cerveny <martin@c-home.cz>
'''

from inspect import currentframe
from flask import Flask
from flask_restful import reqparse, abort, Api, Resource, request
import requests
import sys
import threading
import subprocess
import datetime
import os
import shutil
import re
import socket

import json
import time

CAMEXEC = "/root/jc-cam"

app = Flask(__name__)
api = Api(app)
lock = threading.Lock()

parser_recording = reqparse.RequestParser(bundle_errors=True)
parser_recording.add_argument('recording', type=bool, help='state stop or recording', required=True)

parser_recording_cam = reqparse.RequestParser(bundle_errors=True)
parser_recording_cam.add_argument('srvid', type=int, help='recording server for cam', required=True)
parser_recording_cam.add_argument('ts', type=int, help='milisec timestamp recording server for cam', required=True)

parser_cam = reqparse.RequestParser(bundle_errors=True)
parser_cam.add_argument('mat', type=int, help='matid', required=True)
parser_cam.add_argument('position', type=int, help='position', required=True)
parser_cam.add_argument('ts', type=int, help='timestamp', required=False)

parser_player = reqparse.RequestParser(bundle_errors=True)
parser_player.add_argument('camid', type=int, help='camid', required=True)
parser_player.add_argument('ts', type=int, help='timestamp', required=False)

parser_mat = reqparse.RequestParser(bundle_errors=True)
parser_mat.add_argument('medicals', type=list, location='json', help='medicals', required=False)
parser_mat.add_argument('bookmarks', type=list, location='json', help='bookmarks', required=False)
parser_mat.add_argument('ts', type=int, help='timestamp', required=False)

recording = False
deleting = None
cfgs = {}
cfgslocal = []
today = datetime.datetime.now().strftime('%Y-%m-%d')
hostname = os.uname()[1]
srvid_own = int(hostname[-1])
recordingfile = f"/share/{hostname}/{today}/RECORDING"
pathcache = {}
pathts = {}
cams = {}
srvs = {}
players = {}
mats = {}
MAXMAT = 8
MAXPOS = 4
MAXTS = 999999999999999
CHECKER = 3
RESTPREFIX = "http://srv%d:5000/api/v1"
RESTURIRECORDING = RESTPREFIX + "/recording"
RESTURIRECORDINGCAM = RESTPREFIX + "/recording/%d"
RESTURICAMS = RESTPREFIX + "/cams"
RESTURICAMSDATE = RESTPREFIX + "/cams/%s"
RESTURICAMSDATECAM = RESTPREFIX + "/cams/%s/%d"
RESTURICHUNKSDATE = RESTPREFIX + "/chunks/%s"
RESTURICHUNKSDATECAM = RESTPREFIX + "/chunks/%s/%d"
RESTURIPLAYERS = RESTPREFIX + "/players"
RESTURIPLAYERSPLAYER = RESTPREFIX + "/players/%d"
RESTURIMATSDATE = RESTPREFIX + "/mats/%s"
RESTURIMATSDATEMAT = RESTPREFIX + "/mats/%s/%d"


def get_linenumber():
    cf = currentframe()
    return cf.f_back.f_lineno


def savemats(day, backup=False):
    global cfgslocal, mats
    if not lock.locked():
        print("ERR not locked\n")
    if day not in cfgslocal:
        return
    matsfile = f"/share/{hostname}/{day}/mats.cfg"
    if not os.path.exists(os.path.dirname(matsfile)):
        os.mkdir(os.path.dirname(matsfile), mode=0o755)
    with open(matsfile + "_", "w") as f:
        json.dump(mats[day], f, indent=4)
    if os.path.exists(matsfile):
        if backup:
            os.rename(matsfile, matsfile+'_' + str(int(time.time())))
        else:
            os.remove(matsfile)
    os.rename(matsfile+'_', matsfile)


def loadmats():
    global cfgs, mats, srvs
    if not lock.locked():
        print("ERR not locked\n")
    srvdays = {}
    for day in cfgs.keys():
        daymats = {}
        for matid in [str(matid) for matid in range(1, MAXMAT+1)]:
            daymats[matid] = dict(bookmarks=[], medicals=[])
        matsfile = f"/share/{hostname}/{day}/mats.cfg"
        if os.path.exists(matsfile):
            with open(matsfile, "r") as f:
                try:
                    daymats = json.load(f)
                except:
                    print(f"srv.py: mats json failed {matsfile}")
        mats[day] = daymats
        for srvid in srvs.keys():
            try:
                if "retry" not in srvs[srvid]:
                    if srvid not in srvdays:
                        srvdays[srvid] = requests.get(RESTURICAMS % (srvid), timeout=1).json()
                    if day in srvdays[srvid]:
                        daymats = requests.get(RESTURIMATSDATE % (srvid, day), timeout=1).json()
                        for matid in [str(matid) for matid in range(1, MAXMAT+1)]:
                            if matid not in mats[day]:
                                mats[day][matid] = daymats[matid]
                            else:
                                if "ts" in daymats[matid] and ("ts" not in mats[day][matid] or daymats[matid]["ts"] > mats[day][matid]["ts"]):
                                    mats[day][matid] = daymats[matid]
            except:
                print("srv.py: conn error", get_linenumber())
                srvs[srvid]["retry"] = True
        savemats(day)


class Mats(Resource):
    def get(self, day=None, matid=None):
        global mats
        with lock:
            if matid:
                if 1 <= matid <= MAXMAT and day in mats:
                    return mats[day][str(matid)]
            else:
                if day in mats:
                    return mats[day]
        abort(404, message="bad params")

    def patch(self, day=None, matid=None):
        global mats, srvs
        if matid and day in mats:
            args = parser_mat.parse_args()
            if not socket.gethostbyaddr(request.remote_addr)[0].startswith("srv"):
                args["ts"] = int(time.time())
            with lock:
                for key in ["medicals", "bookmarks", "ts"]:
                    if key in args and args[key] != None:
                        mats[day][str(matid)][key] = args[key]
                savemats(day)
            if not socket.gethostbyaddr(request.remote_addr)[0].startswith("srv"):
                with lock:
                    _srvs = list(srvs.keys())
                for srvid in _srvs:
                    try:
                        requests.patch(RESTURIMATSDATEMAT % (srvid, day, matid), json=args, timeout=1)
                    except:
                        print("srv.py: conn error", get_linenumber())
            return '', 204
        abort(404, message="bad params")


def saveplayers(backup=False):
    global players
    if not lock.locked():
        print("ERR not locked\n")
    playersfile = f"/share/{hostname}/{today}/players.cfg"
    if not os.path.exists(os.path.dirname(playersfile)):
        os.mkdir(os.path.dirname(playersfile), mode=0o755)
    with open(playersfile + "_", "w") as f:
        json.dump(players, f, indent=4)
    if os.path.exists(playersfile):
        if backup:
            os.rename(playersfile, playersfile+'_' + str(int(time.time())))
        else:
            os.remove(playersfile)
    os.rename(playersfile+'_', playersfile)


def loadplayers():
    global players, srvs
    if not lock.locked():
        print("ERR not locked\n")
    players = {}
    playersfile = f"/share/{hostname}/{today}/players.cfg"
    if os.path.exists(playersfile):
        with open(playersfile, "r") as f:
            try:
                players = json.load(f)
            except:
                print(f"srv.py: players json failed {playersfile}")
    for srvid in srvs.keys():
        try:
            for playerid in requests.get(RESTURIPLAYERS % (srvid), timeout=1).json():
                playercfg = requests.get(RESTURIPLAYERSPLAYER % (srvid, int(playerid)), timeout=1).json()
                if playerid not in players:
                    players[playerid] = playercfg
                else:
                    if "ts" in playercfg and ("ts" not in players[playerid] or players[playerid]["ts"] > playercfg["ts"]):
                        players[playerid] = playercfg
        except:
            print("srv.py: conn error", get_linenumber())
            srvs[srvid]["retry"] = True
    saveplayers()


class Players(Resource):
    def get(self, playerid=None):
        global players
        if playerid:
            with lock:
                if str(playerid) in players:
                    return players[str(playerid)]
                else:
                    return {}
        else:
            return list(players.keys())

    def post(self, playerid=None):
        global players, srvs
        if playerid:
            args = parser_player.parse_args()
            if not socket.gethostbyaddr(request.remote_addr)[0].startswith("srv"):
                args["ts"] = int(time.time())
            with lock:
                players[str(playerid)] = args
                saveplayers()
            if not socket.gethostbyaddr(request.remote_addr)[0].startswith("srv"):
                with lock:
                    _srvs = list(srvs.keys())
                for srvid in _srvs:
                    try:
                        requests.post(RESTURIPLAYERSPLAYER % (srvid, playerid), json=args, timeout=1)
                    except:
                        print("srv.py: conn error", get_linenumber())
            return '', 204
        abort(404, message="bad params")


def add_srv(srvid):
    global cfgs, srvs, cams, recording
    if srvid not in srvs or "retry" in srvs[srvid]:
        try:
            _recording = requests.get(RESTURIRECORDING % (srvid), timeout=1).json()["recording"]
            if _recording and not recording:
                requests.put(RESTURIRECORDING % (srvid_own), json=dict(recording=True), timeout=1)
            elif recording and not _recording:
                requests.put(RESTURIRECORDING % (srvid), json=dict(recording=True), timeout=1)
            print(f"srv.py: ADDING srv{srvid}")

            with lock:
                srvs[srvid] = {}
                loadcfg()
                loadplayers()
                loadmats()
                if "retry" in srvs[srvid]:
                    print(f"srv.py: RETRY srv{srvid}")
                    del srvs[srvid]
        except:
            pass


class Recording(Resource):
    def get(self, camid=None):
        global cfgs, srvs, cams, recording
        if camid:
            with lock:
                if camid in cams:
                    return dict(srvid=cams[camid]["srvid"], ts=cams[camid]["ts"])
                else:
                    return dict(srvid=None, ts=MAXTS)
        else:
            return dict(recording=recording)

    def put(self, camid=None):
        global cfgs, srvs, cams, recording
        if camid:
            args = parser_recording_cam.parse_args()
            add_srv(args["srvid"])
            if args["srvid"] in srvs:
                with lock:
                    if camid not in cams or args["ts"] < cams[camid]["ts"]:
                        print(f"srv.py: winner put srv{args['srvid']} cam{camid}")
                        if camid in cams and cams[camid]["process"]:
                            cams[camid]["process"].terminate()
                        cams[camid] = dict(srvid=args["srvid"], ts=args["ts"], process=None, checker=0)
            return '', 204
        else:
            args = parser_recording.parse_args()
            with lock:
                recording = args["recording"]
                if recording:
                    if not os.path.exists(recordingfile):
                        open(recordingfile, "x").close()
                else:
                    if os.path.exists(recordingfile):
                        os.remove(recordingfile)
            if not socket.gethostbyaddr(request.remote_addr)[0].startswith("srv"):
                with lock:
                    _srvs = list(srvs.keys())
                for srvid in _srvs:
                    try:
                        requests.put(RESTURIRECORDING % (srvid), json=args, timeout=1)
                    except:
                        print("srv.py: conn error", get_linenumber())
            return '', 204


def savecfg(backup=False):
    global cfgs, srvs, cams, recording
    if not lock.locked():
        print("ERR not locked\n")
    cfgfile = f"/share/{hostname}/{today}/cams.cfg"
    if not os.path.exists(os.path.dirname(cfgfile)):
        os.mkdir(os.path.dirname(cfgfile), mode=0o755)
    with open(cfgfile + "_", "w") as f:
        json.dump(cfgs[today], f, indent=4)
    if os.path.exists(cfgfile):
        if backup:
            os.rename(cfgfile, cfgfile+'_' + str(int(time.time())))
        else:
            os.remove(cfgfile)
    os.rename(cfgfile+'_', cfgfile)


def loadcfg():
    global cfgs, srvs, cams, recording, cfgslocal
    if not lock.locked():
        print("ERR not locked\n")
    cfgs = {}
    cfgs[today] = {}
    cfgslocal = []
    for day in [day for day in os.listdir(f"/share/{hostname}/") if re.fullmatch(r'^\d{4}-\d{2}-\d{2}$', day) and os.path.exists(f"/share/{hostname}/{day}/cams.cfg")]:
        with open(f"/share/{hostname}/{day}/cams.cfg", "r") as f:
            try:
                cfgs[day] = json.load(f)
                cfgslocal.append(day)
            except:
                print(f"srv.py: cfg json failed /share/{hostname}/{day}/cams.cfg")
    for srvid in srvs.keys():
        try:
            for day in requests.get(RESTURICAMS % (srvid), timeout=1).json():
                cfg = requests.get(RESTURICAMSDATE % (srvid, day), timeout=1).json()
                if day not in cfgs:
                    cfgs[day] = cfg
                else:
                    for camid, cam in cfg.items():
                        if camid not in cfgs[day]:
                            cfgs[day][camid] = cam
                        else:
                            if cfgs[day][camid]["position"] != cam["position"] or cfgs[day][camid]["mat"] != cam["mat"]:
                                print(f"srv.py: cfg differs {cfgs[day][camid]} {cam}")
                                if "ts" in cam and ("ts" not in cfgs[day][camid] or cfgs[day][camid]["ts"] > cam["ts"]):
                                    # replace with newer "ts"
                                    cfgs[day][camid] = cam
                    # resolve colisions
                    movecams = []
                    for camid, cam in cfgs[day].items():
                        for _camid, _cam in cfgs[day].items():
                            if camid != _camid:
                                if cam["position"] == _cam["position"] and cam["mat"] == _cam["mat"]:
                                    print(f"srv.py: cfg collision {camid} {_camid}")
                                    if "ts" in cam and ("ts" not in _cam or _cam["ts"] > cam["ts"]):
                                        movecams.append(camid)
                                        cfgs[day][camid] = dict(mat=0, position=0)

                    for camid in movecams:
                        for (m, p) in [(m, p) for m in range(1, MAXMAT+1) for p in range(1, MAXPOS+1)]:
                            for cam in cfgs[day].values():
                                if cam["mat"] == m and cam["position"] == p:
                                    break
                            else:
                                break
                        cfgs[day][camid] = dict(mat=m, position=p)
        except:
            print("srv.py: conn error", get_linenumber())
            srvs[srvid]["retry"] = True
    savecfg()


class Cam(Resource):
    def get(self, day=None, camid=None):
        global cfgs, srvs, cams, recording
        with lock:
            if day:
                if day in cfgs:
                    return cfgs[day] if not camid else cfgs[day][str(camid)]
                return {}
            else:
                return list(cfgs.keys())
        abort(404, message="bad params")

    def post(self, day=None, camid=None):
        global cfgs, srvs, cams, recording
        if day == today and camid:
            args = parser_cam.parse_args()
            with lock:
                ts = int(time.time())
                for camidswap, cam in cfgs[day].items():
                    if cam["position"] == args["position"] and cam["mat"] == args["mat"]:
                        if str(camid) in cfgs[day]:
                            cfgs[day][camidswap] = cfgs[day][str(camid)]
                            cfgs[day][camidswap]["ts"] = ts
                        else:
                            # fail to swap
                            del cfgs[day][camidswap]
                        break
                cfgs[day][str(camid)] = args
                cfgs[day][str(camid)]["ts"] = ts
                savecfg(backup=True)
            if not socket.gethostbyaddr(request.remote_addr)[0].startswith("srv"):
                with lock:
                    _srvs = list(srvs.keys())
                for srvid in _srvs:
                    try:
                        requests.post(RESTURICAMSDATECAM % (srvid, day, camid), json=args, timeout=1)
                    except:
                        print("srv.py: conn error", get_linenumber())
            return '', 204
        abort(404, message="bad params")


def getpaths(day, camid):
    path = f"/share/{hostname}/{day}/cam{camid:02d}/"
    if os.path.exists(path):
        if path not in pathts or pathts[path] != os.path.getmtime(path):
            pathts[path] = os.path.getmtime(path)
            pathcache[path] = []
            for tsname in [tsname for tsname in os.listdir(f"/share/{hostname}/{day}/cam{camid:02d}/") if re.fullmatch(r'^[0-9a-fA-F]{11}.ts$', tsname)]:
                pathcache[path].append(tsname[:-3])
        return pathcache[path]
    return []


class Chunks(Resource):
    def delete(self, day=None, camid=None):
        global cfgs, srvs, cams, recording, deleting
        if day and not camid:
            with lock:
                if not re.fullmatch(r'^\d{4}-\d{2}-\d{2}$', day) or day not in cfgs:
                    abort(404, message="bad params")

                deleting = day

                if day == today:
                    for camid in cams.keys():
                        if cams[camid]["srvid"] == srvid_own:
                            if cams[camid]["process"]:
                                cams[camid]["process"].terminate()
                        # block restart until rmtree
                        cams[camid] = dict(srvid=None, ts=MAXTS, process=None, checker=0)
                else:
                    del (cfgs[day])
            shutil.rmtree(f"/share/{hostname}/{day}/", ignore_errors=True)
            with lock:
                if day == today:
                    cams = {}
                    savecfg()
                    saveplayers()
                    if recording:
                        open(recordingfile, "x").close()

            if not socket.gethostbyaddr(request.remote_addr)[0].startswith("srv"):
                with lock:
                    _srvs = list(srvs.keys())
                for srvid in _srvs:
                    try:
                        requests.delete(RESTURICHUNKSDATE % (srvid, day), timeout=90)
                    except:
                        print("srv.py: conn error", get_linenumber())
            with lock:
                deleting = None
            return '', 204
        abort(404, message="bad params")

    def get(self, day=None, camid=None):
        global cfgs, srvs, cams, recording
        if day:
            with lock:
                if deleting == day:
                    return []
                if not re.fullmatch(r'^\d{4}-\d{2}-\d{2}$', day) or day not in cfgs:
                    abort(404, message="bad params")
            chlist = []
            if camid:
                chlist.append(dict(srvid=srvid_own, camid=camid, ts=getpaths(day, camid)))
            else:
                for camid in [int(camname[-2:]) for camname in os.chlistdir(f"/share/{hostname}/{day}/") if re.fullmatch(r'^cam\d{2}$', camname)]:
                    chlist.append(dict(srvid=srvid_own, camid=camid, ts=getpaths(day, camid)))

            if not socket.gethostbyaddr(request.remote_addr)[0].startswith("srv"):
                with lock:
                    _srvs = list(srvs.keys())
                for srvid in _srvs:
                    if day not in srvs[srvid]:
                        srvs[srvid][day] = {}
                    if day != today and srvs[srvid][day] and (not camid or camid in srvs[srvid][day]):
                        # use cache
                        if camid:
                            chlist.append(dict(srvid=srvid, camid=camid, ts=srvs[srvid][day][camid]))
                        else:
                            for camid in srvs[srvid][day].keys():
                                chlist.append(dict(srvid=srvid, camid=camid, ts=srvs[srvid][day][camid]))
                    else:
                        try:
                            if camid:
                                response = requests.get(RESTURICHUNKSDATECAM % (srvid, day, camid), timeout=1)
                                line = response.json()[0]
                                srvs[srvid][day][camid] = line["ts"]
                                if line["srvid"] != srvid or line["camid"] != camid:
                                    print(f"srv.py: ERR srv/camid not match {line['srvid']} {srvid} {line['camid']} {camid}")
                                chlist.extend(response.json())
                            else:
                                response = requests.get(RESTURICHUNKSDATE % (srvid, day), timeout=1)
                                for line in response.json():
                                    srvs[srvid][day][line["camid"]] = line["ts"]
                                    if line["srvid"] != srvid:
                                        print(f"srv.py: ERR srv not match {line['srvid']} {srvid}")
                                chlist.extend(response.json())
                        except:
                            print("srv.py: conn error", get_linenumber())
            return chlist
        abort(404, message="bad params")


api.add_resource(Recording, '/api/v1/recording', '/api/v1/recording/<int:camid>')
api.add_resource(Cam, '/api/v1/cams', '/api/v1/cams/<string:day>', '/api/v1/cams/<string:day>/<int:camid>')
api.add_resource(Chunks, '/api/v1/chunks/<string:day>', '/api/v1/chunks/<string:day>/<int:camid>')
api.add_resource(Players, '/api/v1/players', '/api/v1/players/<int:playerid>')
api.add_resource(Mats, '/api/v1/mats/<string:day>', '/api/v1/mats/<string:day>/<int:matid>')


def live_thread():
    global cfgs, srvs, cams, recording
    print("LIVE thread start")
    while True:
        for srvid in range(1, 9):
            if srvid == srvid_own:
                continue
            if subprocess.run(["/usr/bin/ping", "-c", "1", "-W", "0.1", f"srv{srvid:d}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                add_srv(srvid)
            if srvid in srvs:
                try:
                    requests.get(RESTURICAMS % (srvid), timeout=1)
                except:
                    print(f"srv.py: DELETING srv{srvid}")
                    with lock:
                        for camid in [camid for camid in cams if cams[camid]["srvid"] == srvid]:
                            del cams[camid]
                        del srvs[srvid]
                        loadcfg()
                        loadplayers()
                        loadmats()
            time.sleep(0.1)

        for camid in range(1, 33):
            if subprocess.run(["/usr/bin/ping", "-c", "1", "-W", "0.1", f"cam{camid:02d}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                if camid not in cams:
                    print(f"srv.py: ADD cam{camid:02d}")
                    win_srvid = None
                    win_ts = MAXTS

                    # first try do receive actual cam streaming
                    with lock:
                        _srvs = list(srvs.keys())
                    for srvid in _srvs:
                        try:
                            camrec = requests.get(RESTURIRECORDINGCAM % (srvid, camid), timeout=1).json()
                            if camrec["ts"] < win_ts:
                                win_srvid = camrec["srvid"]
                                win_ts = camrec["ts"]
                        except:
                            print("srv.py: conn error", get_linenumber())
                    if win_srvid:
                        # found actual cam streaming
                        with lock:
                            cams[camid] = dict(srvid=win_srvid, ts=win_ts, process=None, checker=0)
                        print(f"srv.py: scan winner srv{win_srvid} cam{camid}")
                    else:
                        # create new cam streaming
                        with lock:
                            ts = int(time.time()*1000)*10+srvid_own
                            cams[camid] = dict(srvid=srvid_own, ts=ts, process=None, checker=CHECKER)

                        # push srvid_our if not overriden
                        with lock:
                            _srvs = list(srvs.keys())
                        for srvid in _srvs:
                            if cams[camid]["srvid"] == srvid_own:
                                try:
                                    requests.put(RESTURIRECORDINGCAM % (srvid, camid), json=dict(srvid=srvid_own, ts=ts), timeout=1)
                                except:
                                    print("srv.py: conn error", get_linenumber())

                # check for validity
                if cams[camid]["checker"] > 0:
                    cams[camid]["checker"] -= 1
                    if cams[camid]["srvid"] == srvid_own:
                        with lock:
                            _srvs = list(srvs.keys())
                        for srvid in _srvs:
                            try:
                                response = requests.get(RESTURIRECORDINGCAM % (srvid, camid), timeout=1)
                                if response.json()["srvid"] != srvid_own:
                                    print(f"srv.py: arbitration collision on srv{srvid} is srv{response.json()['srvid']} cam{camid}")
                                    with lock:
                                        del cams[camid]
                                    break
                            except:
                                print("srv.py: conn error", get_linenumber())
                        else:
                            print(f"srv.py: arbitration winner srv{cams[camid]['srvid']} cam{camid}")
                    else:
                        cams[camid]["checker"] = 0
                        print(f"srv.py: arbitration remote winner srv{cams[camid]['srvid']} cam{camid}")

                if camid in cams:
                    with lock:
                        # extend config for new cam if needed
                        if str(camid) not in cfgs[today]:
                            loadcfg()
                            if str(camid) not in cfgs[today]:
                                for (m, p) in [(m, p) for m in range(1, MAXMAT+1) for p in range(1, MAXPOS+1)]:
                                    for cam in cfgs[today].values():
                                        if cam["mat"] == m and cam["position"] == p:
                                            break
                                    else:
                                        break
                                cfgs[today][str(camid)] = dict(mat=m, position=p)
                                savecfg()

                        # check and start/stop
                        (m, p) = (cfgs[today][str(camid)]["mat"], cfgs[today][str(camid)]["position"])
                        if recording:
                            if cams[camid]["srvid"] == srvid_own and cams[camid]["checker"] == 0 and (not cams[camid]["process"] or cams[camid]["process"].poll()):
                                print(f"srv.py: START cam{camid:02d}")
                                cams[camid]["process"] = subprocess.Popen([CAMEXEC, f"/share/{hostname}/{today}/", f"cam{camid:02d}", f"{m}", f"{p}"])
                        else:
                            if cams[camid]["process"]:
                                print(f"srv.py: STOP cam{camid:02d}")
                                cams[camid]["process"].terminate()
                                cams[camid]["process"] = None
            else:
                with lock:
                    if camid in cams:
                        print(f"srv.py: DELETE cam{camid:02d}")
                        if cams[camid]["process"]:
                            cams[camid]["process"].terminate()
                        del cams[camid]
            time.sleep(0.1)


if __name__ == '__main__':
    print("VERSION v1.2024-12-10")
    recording = os.path.exists(recordingfile)
    with lock:
        if not os.path.exists(f"/share/{hostname}/{today}/cams.cfg"):
            cfgs[today] = {}
        loadcfg()
        loadplayers()
        loadmats()

    livetid = threading.Thread(target=live_thread)
    livetid.start()

    app.run(threaded=True, host='0.0.0.0')
