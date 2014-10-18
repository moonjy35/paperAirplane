import logging
import socket
import json
import tempfile
import base64
import threading
import database
import time
import Queue
import os
import sys

class IncommingJob():
    def __init__(self, con, addr, toRelease):
        self.toRelease = toRelease
        self.con = con
        self.addr = addr

        self.logger = logging.getLogger("JobHandler")

        jobRaw = self.getJob()
        jid = self.saveJob(jobRaw)
        self.sendToBilling(jid)

    def getJob(self):
        self.logger.debug("Processing new job from %s", self.addr[0])
        self.con.settimeout(1)
        data = self.con.recv(256)
        jobJSON = data
        while(len(data) != 0):
            data = self.con.recv(256)
            jobJSON += data
        job = json.loads(base64.b64decode(jobJSON))
        self.logger.info("Recieved job %s from %s on %s", job["name"], job["originUser"], job["originPrinter"])
        return job

    def sendToBilling(self, jid):
        self.logger.debug("Sending %s for job release", jid)
        self.toRelease.put(jid)

    def saveJob(self, job):
        jid = job["name"]
        spoolFile = open(jid, 'w')
        json.dump(job, spoolFile)
        spoolFile.close()
        return jid

class Spooler():
    def __init__(self, config, queues):
        self.threadOps = queues["threadControl"]
        self.toRelease = queues["toRelease"]

        bindaddr = config["spooler"]["bindAddr"]
        bindport = config["spooler"]["bindPort"]
        spooldir = config["global"]["spoolDir"]

        self.logger = logging.getLogger("CoreSpooler")

        #need to improve this
        # currently it moves us into the spooler's main directory
        self.logger.debug("Current path is %s", os.getcwd())
        if(spooldir not in os.getcwd()):
            try:
                self.logger.info("Pivoting to master spool directory")
                os.chdir(spooldir)
                self.logger.debug("Successfully found master spool directory")
            except OSError:
                self.logger.warning("Could not use master spool directory")
                self.logger.warning("Attempting to create new spool directory")
                os.mkdir(spooldir)
                os.chdir(spooldir)
                self.logger.info("Successfully found master spool directory")
        else:
            self.logger.debug("Already in spooldir")

        # attempt to bind the master spooler onto a port
        try:
            self.logger.info("Initializing master spooler on %s:%s", bindaddr, bindport)
            self.s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.s.bind((bindaddr, bindport))
        except Exception as e:
            self.logger.exception("Could not bind: %s", e)

        #clear thread lock
        self.threadOps.get(False)
        self.threadOps.task_done()
        self.run()

    def listener(self):
        self.s.listen(5)
        con, addr = self.s.accept()
        t = threading.Thread(target=IncommingJob, args=(con, addr, self.toRelease))
        t.daemon = True
        t.start()

    def run(self):
        while(True):
            self.listener()

class PSParser():
    def __init__(self):
        self.logger = logging.getLogger("PSParser")
        self.logger.info("Loaded PostScript Parser")
        
    def __getPSFromJID(self, jid):
        jobFile = open(jid, 'r')
        job = json.load(jobFile)
        jobFile.close()
        return job["postscript"]

    def isDuplex(self, jid):
        ps = self.__getPSFromJID(jid)
        if("/Duplex true" in ps):
            self.logger.debug("job %s is duplex enabled", jid)
            return True
        else:
            self.logger.debug("job %s is not duplex enabled", jid)
            return False

    def pageCount(self, jid):
        ps = self.__getPSFromJID(jid)
        self.logger.debug("Computing page count for %s", jid)
        numPages = ps.count("%%Page:")
        return numPages

class Billing():
    def __init__(self, config, queues):
        self.threadOps = queues["threadControl"]
        self.toBill = queues["toBill"]
        self.toPrint = queues["toPrint"]

        dbpath = config["billing"]["path"]

        self.logger = logging.getLogger("Billing")

        #init some internal instances of stuff
        self.logger.info("Initializing Billing Manager")

        self.logger.debug("Attempting to connect to database")
        self.db = database.BillingDB(dbpath)
        self.logger.debug("Successfully connected to database!")

        self.logger.debug("Attempting to create a parser")
        self.parser = PSParser()
        self.logger.debug("Successfully created parser")

        #enter main loop
        self.run()

    def run(self):
        while(True):
            jid = self.toBill.get(block=True)
            cost = self.computeCost(jid)
            user = self.getUser(jid)
            self.logger.info("Billing user %s %s credit(s) for job %s", user, cost, jid)
            self.logger.debug("Forwarding %s to print manager", jid)
            self.toPrint.put(jid)

    def computeCost(self, jid):
        cost = self.parser.pageCount(jid)
        if self.parser.isDuplex(jid):
            cost = ceiling(cost / 2)
        return cost

    def getUser(self, jid):
        f = open(jid, 'r')
        j = json.load(f)
        user = j["originUser"]
        f.close()
        return user

class JobRelease():
    def __init__(self, config, queues):
        self.threadOps = queues["threadControl"]
        self.toRelease = queues["toRelease"]
        self.toBill = queues["toBill"]

        self.logger = logging.getLogger("JobRelease")

        self.run()

    def run(self):
        #release all jobs immeadiately, need to rewrite at some point
        while(True):
            jid = self.toRelease.get(block=True)
            self.logger.debug("Forwarding job %s for billing", jid)
            self.toBill.put(jid)
            

class SendToPrinter():
    def __init__(self, config, queues):
        self.threadOps = queues["threadControl"]
        self.toPrint = queues["toPrint"]
        self.config = config

        self.logger = logging.getLogger("PrinterOutput")

        self.run()

    def run(self):
        while(True):
            jid = self.toPrint.get(block=True)
            self.logger.debug("Got print request for job %s", jid)
            self.printJob(jid)
            self.logger.debug("Printed job %s", jid)
            self.rmJob(jid)

    def printJob(self, jid):
        destPrinter = self.getDestPrinter(jid)
        printer = self.config["printers"][destPrinter]["address"]
        port =  self.config["printers"][destPrinter]["port"]
        self.logger.debug("Sending %s to %s", jid, destPrinter)

        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            ps = self.getPS(jid)
            s.connect((printer, port))
            #s.sendall(ps)
            s.close()
        except Exception as e:
            self.logger.critical("Encountered error while printing: %s", e)

    def getDestPrinter(self, jid):
        f = open(jid)
        j = json.load(f)
        f.close()
        return j["destPrinter"]

    def getPS(self, jid):
        f = open(jid)
        j = json.load(f)
        f.close()
        return j["postscript"]

    def rmJob(self, jid):
        self.logger.debug("Removing spool file for %s", jid)
        os.remove(jid)
        self.logger.info("Completed handling of %s", jid)

class CentralControl():
    def __init__(self):
        self.logger = logging.getLogger("CentralControl")

        self.logger.info("Initializing CentralControl")
        self.logger.debug("Attempting to get config")
        config = self.getConfig()
        self.logger.debug("Successfully got config")

        self.queues = {}
        self.queues["threadControl"] = Queue.Queue()
        self.queues["toBill"] = Queue.Queue()
        self.queues["toRelease"] = Queue.Queue()
        self.queues["toPrint"] = Queue.Queue()


        self.threads = []
        self.threads.append(threading.Thread(target=Spooler, args=(config, self.queues)))
        self.threads.append(threading.Thread(target=Billing, args=(config, self.queues)))
        self.threads.append(threading.Thread(target=JobRelease, args=(config, self.queues)))
        self.threads.append(threading.Thread(target=SendToPrinter, args=(config, self.queues)))

        #set up startup locks before running:
        self.queues["threadControl"].put("spoolerStartup")


    def getConfig(self):
        try:
            configFile = open("config.json")
            conf = json.load(configFile)
            configFile.close()
            return conf
        except Exception as e:
            self.logger.critical("Malformed config file: %s", e)
            sys.exit(1)

    def run(self):
        self.logger.info("GOING POLYTHREADED")
        for thread in self.threads:
            thread.daemon = True
            thread.start()

        # we need to keep this thread running to check thread status
        while(True):
            if not any([thread.isAlive() for thread in self.threads]):
                break
            else:
                time.sleep(1)

        self.logger.info("All threads have exited, now exiting program")

if __name__ == "__main__":
    logging.basicConfig(level = logging.DEBUG)
    logging.info("Starting in debug mode")
    test = CentralControl()
    test.run()