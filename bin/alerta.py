#!/usr/bin/env python
########################################
#
# alerta.py - Alert Server Module
#
########################################

import os
import sys
import time
try:
    import json
except ImportError:
    import simplejson as json
import yaml
import threading
from Queue import Queue
import stomp
import pymongo
import datetime
import pytz
import logging
import re

__program__ = 'alerta'
__version__ = '1.6.0'

BROKER_LIST  = [('localhost', 61613)] # list of brokers for failover
ALERT_QUEUE  = '/queue/alerts' # inbound
NOTIFY_TOPIC = '/topic/notify' # outbound
LOGGER_QUEUE = '/queue/logger' # outbound

DEFAULT_TIMEOUT = 86400 # expire OPEN alerts after 1 day
EXPIRATION_TIME = 600 # seconds = 10 minutes

LOGFILE = '/var/log/alerta/alerta.log'
PIDFILE = '/var/run/alerta/alerta.pid'
ALERTCONF = '/opt/alerta/conf/alerta.yaml'
PARSERDIR = '/opt/alerta/bin/parsers'

NUM_THREADS = 4

# Global variables
conn = None
db = None
alerts = None
mgmt = None
queue = Queue()

# Extend JSON Encoder to support ISO 8601 format dates
class DateEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (datetime.date, datetime.datetime)):
            return obj.replace(microsecond=0).isoformat() + ".%03dZ" % (obj.microsecond//1000)
        else:
            return json.JSONEncoder.default(self, obj)

class WorkerThread(threading.Thread):

    def __init__(self, queue):
        threading.Thread.__init__(self)
        self.input_queue = queue

    def run(self):
        global db, alerts, mgmt, hb, conn, queue

        while True:
            alert = self.input_queue.get()
            if not alert:
                logging.info('%s is shutting down.', self.getName())
                break

            start = time.time()
            alertid = alert['id']
            logging.info('%s : %s', alertid, alert['summary'])

            # Load alert transforms
            try:
                alertconf = yaml.load(open(ALERTCONF))
                logging.info('Loaded %d alert transforms and blackout rules OK', len(alertconf))
            except Exception, e:
                alertconf = dict()
                logging.warning('Failed to load alert transforms and blackout rules: %s', e)

            # Apply alert transforms and blackouts
            suppress = False
            for conf in alertconf:
                logging.debug('alertconf: %s', conf)
                if all(item in alert.items() for item in conf['match'].items()):
                    if 'parser' in conf:
                        logging.debug('Loading parser %s', conf['parser'])
                        try:
                            exec(open('%s/%s.py' % (PARSERDIR, conf['parser']))) in globals(), locals()
                            logging.info('Parser %s/%s exec OK', PARSERDIR, conf['parser'])
                        except Exception, e:
                            logging.warning('Parser %s failed: %s', conf['parser'], e)
                    if 'event' in conf:
                        event = conf['event']
                    if 'resource' in conf:
                        resource = conf['resource']
                    if 'severity' in conf:
                        severity = conf['severity']
                    if 'group' in conf:
                        group = conf['group']
                    if 'value' in conf:
                        value = conf['value']
                    if 'text' in conf:
                        text = conf['text']
                    if 'environment' in conf:
                        environment = [conf['environment']]
                    if 'service' in conf:
                        service = [conf['service']]
                    if 'tags' in conf:
                        tags = conf['tags']
                    if 'correlatedEvents' in conf:
                        correlate = conf['correlatedEvents']
                    if 'thresholdInfo' in conf:
                        threshold = conf['thresholdInfo']
                    if 'suppress' in conf:
                        suppress = conf['suppress']
                    break

            if suppress:
                logging.info('%s : Suppressing alert %s', alert['id'], alert['summary'])
                return

            createTime = datetime.datetime.strptime(alert['createTime'], '%Y-%m-%dT%H:%M:%S.%fZ')
            createTime = createTime.replace(tzinfo=pytz.utc)

            receiveTime = datetime.datetime.strptime(alert['receiveTime'], '%Y-%m-%dT%H:%M:%S.%fZ')
            receiveTime = receiveTime.replace(tzinfo=pytz.utc)

            # Add expire timestamp
            if 'timeout' in alert and alert['timeout'] == 0:
                expireTime = ''
            elif 'timeout' in alert and alert['timeout'] > 0:
                expireTime = createTime + datetime.timedelta(seconds=alert['timeout'])
            else:
                alert['timeout'] = DEFAULT_TIMEOUT
                expireTime = createTime + datetime.timedelta(seconds=alert['timeout'])

            if alerts.find_one({"environment": alert['environment'], "resource": alert['resource'], "event": alert['event'], "severity": alert['severity']}):
                logging.info('%s : Duplicate alert -> update dup count', alertid)
                # Duplicate alert .. 1. update existing document with lastReceiveTime, lastReceiveId, text, summary, value, tags and origin
                #                    2. increment duplicate count

                # FIXME - no native find_and_modify method in this version of pymongo
                no_obj_error = "No matching object found"
                alert = db.command("findAndModify", 'alerts',
                    allowable_errors=[no_obj_error],
                    query={ "environment": alert['environment'], "resource": alert['resource'], "event": alert['event'] },
                    update={ '$set': { "lastReceiveTime": receiveTime, "expireTime": expireTime,
                                "lastReceiveId": alertid, "text": alert['text'], "summary": alert['summary'], "value": alert['value'],
                                "tags": alert['tags'], "repeat": True, "origin": alert['origin'] },
                      '$inc': { "duplicateCount": 1 }},
                    new=True,
                    fields={ "history": 0 })['value']

                if alert['status'] not in ['OPEN','ACK','CLOSED']:
                    if alert['severity'] != 'NORMAL':
                        status = 'OPEN'
                    else:
                        status = 'CLOSED'
                else:
                    status = None

                if status:
                    alert['status'] = status
                    updateTime = datetime.datetime.utcnow()
                    updateTime = updateTime.replace(tzinfo=pytz.utc)
                    alerts.update(
                        { "environment": alert['environment'], "resource": alert['resource'], '$or': [{"event": alert['event']}, {"correlatedEvents": alert['event']}]},
                        { '$set': { "status": status },
                          '$push': { "history": { "status": status, "updateTime": updateTime } }})
                    logging.info('%s : Alert status for duplicate %s %s alert changed to %s', alertid, alert['severity'], alert['event'], status)
                else:
                    logging.info('%s : Alert status for duplicate %s %s alert unchanged because either OPEN, ACK or CLOSED', alertid, alert['severity'], alert['event'])

                self.input_queue.task_done()

            elif alerts.find_one({"environment": alert['environment'], "resource": alert['resource'], '$or': [{"event": alert['event']}, {"correlatedEvents": alert['event']}]}):
                previousSeverity = alerts.find_one({"environment": alert['environment'], "resource": alert['resource'], '$or': [{"event": alert['event']}, {"correlatedEvents": alert['event']}]}, { "severity": 1 , "_id": 0})['severity']
                logging.info('%s : Event and/or severity change %s %s -> %s update details', alertid, alert['event'], previousSeverity, alert['severity'])
                # Diff sev alert ... 1. update existing document with severity, createTime, receiveTime, lastReceiveTime, previousSeverity,
                #                        severityCode, lastReceiveId, text, summary, value, tags and origin
                #                    2. set duplicate count to zero
                #                    3. push history

                # FIXME - no native find_and_modify method in this version of pymongo
                no_obj_error = "No matching object found"
                alert = db.command("findAndModify", 'alerts',
                    allowable_errors=[no_obj_error],
                    query={ "environment": alert['environment'], "resource": alert['resource'], '$or': [{"event": alert['event']}, {"correlatedEvents": alert['event']}]},
                    update={ '$set': { "event": alert['event'], "severity": alert['severity'], "severityCode": alert['severityCode'],
                               "createTime": createTime, "receiveTime": receiveTime, "lastReceiveTime": receiveTime, "expireTime": expireTime,
                               "previousSeverity": previousSeverity, "lastReceiveId": alertid, "text": alert['text'], "summary": alert['summary'], "value": alert['value'],
                               "tags": alert['tags'], "repeat": False, "origin": alert['origin'], "thresholdInfo": alert['thresholdInfo'], "duplicateCount": 0 },
                             '$push': { "history": { "createTime": createTime, "receiveTime": receiveTime, "severity": alert['severity'], "event": alert['event'],
                               "severityCode": alert['severityCode'], "value": alert['value'], "text": alert['text'], "id": alertid }}},
                    new=True,
                    fields={ "history": 0 })['value']

                # Update alert status
                status = None

                if alert['severity'] in ['DEBUG','INFORM']:
                    status = 'OPEN'
                elif alert['severity'] == 'NORMAL':
                    status = 'CLOSED'
                elif alert['severity'] == 'WARNING':
                    if previousSeverity in ['NORMAL']:
                        status = 'OPEN'
                elif alert['severity'] == 'MINOR':
                    if previousSeverity in ['NORMAL','WARNING']:
                        status = 'OPEN'
                elif alert['severity'] == 'MAJOR':
                    if previousSeverity in ['NORMAL','WARNING','MINOR']:
                        status = 'OPEN'
                elif alert['severity'] == 'CRITICAL':
                    if previousSeverity in ['NORMAL','WARNING','MINOR','MAJOR']:
                        status = 'OPEN'
                else:
                    status = 'UNKNOWN'

                if status:
                    alert['status'] = status
                    updateTime = datetime.datetime.utcnow()
                    updateTime = updateTime.replace(tzinfo=pytz.utc)
                    alerts.update(
                        { "environment": alert['environment'], "resource": alert['resource'], '$or': [{"event": alert['event']}, {"correlatedEvents": alert['event']}]},
                        { '$set': { "status": status },
                          '$push': { "history": { "status": status, "updateTime": updateTime } }})
                    logging.info('%s : Alert status for %s %s alert with diff event/severity changed to %s', alertid, alert['severity'], alert['event'], status)

                # Forward alert to notify topic and logger queue
                while not conn.is_connected():
                    logging.warning('Waiting for message broker to become available')
                    time.sleep(1.0)

                # Use object id as canonical alert id
                alert['id'] = alert['_id']
                del alert['_id']

                headers = dict()
                headers['type']           = alert['type']
                headers['correlation-id'] = alert['id']

                logging.info('%s : Fwd alert to %s', alert['id'], NOTIFY_TOPIC)
                try:
                    conn.send(json.dumps(alert, cls=DateEncoder), headers, destination=NOTIFY_TOPIC)
                except Exception, e:
                    logging.error('Failed to send alert to broker %s', e)

                logging.info('%s : Fwd alert to %s', alert['id'], LOGGER_QUEUE)
                try:
                    conn.send(json.dumps(alert, cls=DateEncoder), headers, destination=LOGGER_QUEUE)
                except Exception, e:
                    logging.error('Failed to send alert to broker %s', e)

                self.input_queue.task_done()
                logging.info('%s : Alert forwarded to %s and %s', alert['id'], NOTIFY_TOPIC, LOGGER_QUEUE)

            else:
                logging.info('%s : New alert -> insert', alertid)
                # New alert so ... 1. insert entire document
                #                  2. push history
                #                  3. set duplicate count to zero

                # Use alert id as object id
                alertid = alert['id']
                alert['_id'] = alertid
                del alert['id']

                alert['lastReceiveId']    = alertid
                alert['createTime']       = createTime
                alert['receiveTime']      = receiveTime
                alert['lastReceiveTime']  = receiveTime
                alert['expireTime']       = expireTime
                alert['previousSeverity'] = 'UNKNOWN'
                alert['repeat']           = False
                if alert['severity'] != 'NORMAL':
                    status = 'OPEN'
                else:
                    status = 'CLOSED'
                alert['status'] = status

                alerts.insert(alert, safe=True)
                alerts.update(
                    { "environment": alert['environment'], "resource": alert['resource'], "event": alert['event'] },
                    { '$push': { "history": { "createTime": createTime, "receiveTime": receiveTime, "severity": alert['severity'], "event": alert['event'],
                                 "severityCode": alert['severityCode'], "value": alert['value'], "text": alert['text'], "id": alertid }},
                      '$set': { "duplicateCount": 0 }}, safe=True)

                updateTime = datetime.datetime.utcnow()
                updateTime = updateTime.replace(tzinfo=pytz.utc)
                alerts.update(
                    { "environment": alert['environment'], "resource": alert['resource'], "event": alert['event'] },
                    { '$set': { "status": status },
                      '$push': { "history": { "status": status, "updateTime": updateTime } }}, safe=True)
                logging.info('%s : Alert status for new %s %s alert set to %s', alertid, alert['severity'], alert['event'], status)

                # Forward alert to notify topic and logger queue
                while not conn.is_connected():
                    logging.warning('Waiting for message broker to become available')
                    time.sleep(1.0)

                alert = alerts.find_one({"_id": alertid}, {"_id": 0, "history": 0})
                alert['id'] = alertid

                headers = dict()
                headers['type']           = alert['type']
                headers['correlation-id'] = alert['id']

                logging.info('%s : Fwd alert to %s', alert['id'], NOTIFY_TOPIC)
                try:
                    conn.send(json.dumps(alert, cls=DateEncoder), headers, destination=NOTIFY_TOPIC)
                except Exception, e:
                    logging.error('Failed to send alert to broker %s', e)

                logging.info('%s : Fwd alert to %s', alert['id'], LOGGER_QUEUE)
                try:
                    conn.send(json.dumps(alert, cls=DateEncoder), headers, destination=LOGGER_QUEUE)
                except Exception, e:
                    logging.error('Failed to send alert to broker %s', e)

                self.input_queue.task_done()
                logging.info('%s : Alert forwarded to %s and %s', alert['id'], NOTIFY_TOPIC, LOGGER_QUEUE)

            # Update management stats
            proc_latency = int((time.time() - start) * 1000)
            mgmt.update(
                { "group": "alerts", "name": "processed", "type": "timer", "title": "Alert process rate and duration", "description": "Time taken to process the alert" },
                { '$inc': { "count": 1, "totalTime": proc_latency}},
               True)
            delta = receiveTime - createTime
            recv_latency = int(delta.days * 24 * 60 * 60 * 1000 + delta.seconds * 1000 + delta.microseconds / 1000)
            mgmt.update(
                { "group": "alerts", "name": "received", "type": "timer", "title": "Alert receive rate and latency", "description": "Time taken for alert to be received by the server" },
                { '$inc': { "count": 1, "totalTime": recv_latency}},
               True)
            queue_len = queue.qsize()
            mgmt.update(
                { "group": "alerts", "name": "queue", "type": "gauge", "title": "Alert internal queue length", "description": "Length of internal alert queue" },
                { '$set': { "value": queue_len }},
               True)
            logging.info('%s : Alert receive latency = %s ms, process latency = %s ms, queue length = %s', alertid, recv_latency, proc_latency, queue_len)

            heartbeatTime = datetime.datetime.utcnow()
            heartbeatTime = heartbeatTime.replace(tzinfo=pytz.utc)
            hb.update(
                { "origin": "%s/%s" % (__program__, os.uname()[1]) },
                { "origin": "%s/%s" % (__program__, os.uname()[1]), "version": __version__, "createTime": heartbeatTime, "receiveTime": heartbeatTime },
                True)

        self.input_queue.task_done()
        return

class MessageHandler(object):

    def on_error(self, headers, body):
        logging.error('Received an error %s', body)

    def on_message(self, headers, body):
        global hb, queue

        logging.debug("Received alert : %s", body)

        alert = dict()
        try:
            alert = json.loads(body)
        except ValueError, e:
            logging.error("Could not decode JSON - %s", e)
            return

        # Set receiveTime
        receiveTime = datetime.datetime.utcnow()
        alert['receiveTime'] = receiveTime.replace(microsecond=0).isoformat() + ".%03dZ" % (receiveTime.microsecond//1000)

        # Get createTime
        createTime = datetime.datetime.strptime(alert['createTime'], '%Y-%m-%dT%H:%M:%S.%fZ')
        createTime = createTime.replace(tzinfo=pytz.utc)

        # Handle heartbeats
        if alert['type'] == 'heartbeat':
            hb.update(
                { "origin": alert['origin'] },
                { "origin": alert['origin'], "version": alert['version'], "createTime": createTime, "receiveTime": receiveTime },
                True)
            logging.info('%s : heartbeat from %s', alert['id'], alert['origin'])
            return

        # Queue alert for processing
        queue.put(alert)

    def on_disconnected(self):
        global conn

        logging.warning('Connection lost. Attempting auto-reconnect to %s', ALERT_QUEUE)
        conn.start()
        conn.connect(wait=True)
        conn.subscribe(destination=ALERT_QUEUE, ack='auto')

def main():
    global db, alerts, mgmt, hb, conn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s alerta[%(process)d] %(threadName)s %(levelname)s - %(message)s", filename=LOGFILE)
    logging.info('Starting up Alerta version %s', __version__)

    # Write pid file if not already running
    if os.path.isfile(PIDFILE):
        pid = open(PIDFILE).read()
        try:
            os.kill(int(pid), 0)
            logging.error('Process with pid %s already exists, exiting', pid)
            sys.exit(1)
        except OSError:
            pass
    file(PIDFILE, 'w').write(str(os.getpid()))

    # Connection to MongoDB
    try:
        mongo = pymongo.Connection()
        db = mongo.monitoring
        alerts = db.alerts
        mgmt = db.status
        hb = db.heartbeats
    except pymongo.errors.ConnectionFailure, e:
        logging.error('Mongo connection failure: %s', e)
        sys.exit(1)

    # Connect to message broker
    try:
        conn = stomp.Connection(
                   BROKER_LIST,
                   reconnect_sleep_increase = 5.0,
                   reconnect_sleep_max = 120.0,
                   reconnect_attempts_max = 20
               )
        conn.set_listener('', MessageHandler())
        conn.start()
        conn.connect(wait=True)
        conn.subscribe(destination=ALERT_QUEUE, ack='auto')
    except Exception, e:
        logging.error('Stomp connection error: %s', e)

    # Start worker thread
    for i in range(NUM_THREADS):
        w = WorkerThread(queue)
        w.start()
        logging.info('Starting alert forwarding thread: %s', w.getName())

    while True:
        try:
            time.sleep(0.01)
        except (KeyboardInterrupt, SystemExit):
            for i in range(NUM_THREADS):
                queue.put(None)
            conn.disconnect()
            os.unlink(PIDFILE)
            sys.exit(0)

if __name__ == '__main__':
    main()
