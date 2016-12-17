'''
Argus probe for the Beamlogic Site Analyzer Lite
http://www.beamlogic.com/products/802154-site-analyzer.aspx
'''

import time
import struct
import socket
import threading
import json
import Queue

import paho.mqtt.publish

import ArgusVersion

class AppData(object):
    pass

class PublishThread(threading.Thread):
    '''
    Thread which publishes sniffed frames to the MQTT broker.
    '''
    
    MQTT_BROKER_HOST    = 'broker.hivemq.com'
    MQTT_BROKER_PORT    = 1883
    MQTT_BROKER_TOPIC   = 'daumesnil'
    
    def __init__(self):
        
        # local variables
        self.txQueue         = Queue.Queue(maxsize=10)
        
        # start the thread
        threading.Thread.__init__(self)
        self.name            = 'SnifferThread'
        self.start()
    
    def run(self):
        try:
            while True:
                # wait for first packet
                msgs = [self.txQueue.get(),]
                
                # get other packets (if any)
                try:
                    while True:
                        msgs += [self.txQueue.get(block=False)]
                except Queue.Empty:
                    pass
                
                # add topic
                msgs = [
                    {
                        'topic':       'argus/{0}'.format(self.MQTT_BROKER_TOPIC),
                        'payload':     m,
                    } for m in msgs
                ]
                
                # publish
                paho.mqtt.publish.multiple(
                    msgs,
                    hostname     = self.MQTT_BROKER_HOST,
                    port         = self.MQTT_BROKER_PORT,
                )
                
        except Exception as err:
            print err
    
    #======================== public ==========================================
    
    def publishFrame(self,frame):
        msg = {
            'description':   'zep',
            'device':        'Beamlogic',
            'bytes':         ''.join(['{0:02x}'.format(b) for b in frame]),
        }
        try:
            self.txQueue.put(json.dumps(msg),block=False)
        except Queue.Full:
            print "WARNING transmit queue to MQTT broker full. Dropping packet."
    
    #======================== private =========================================
    

class SnifferThread(threading.Thread):
    '''
    Thread which attaches to the sniffer and parses incoming frames.
    '''
    
    PCAP_GLOBALHEADER_LEN    = 24 # 4+2+2+4+4+4+4
    PCAP_PACKETHEADER_LEN    = 16 # 4+4+4+4
    BEAMLOGIC_HEADER_LEN     = 18 # 8+1+1+4+4
    PIPE_SNIFFER             = r'\\.\pipe\analyzer'
    
    def __init__(self,publishThread):
        
        # store params
        self.publishThread             = publishThread
        
        # local variables
        self.dataLock                  = threading.Lock()
        self.rxBuffer                  = []
        self.doneReceivingGlobalHeader = False
        self.doneReceivingPacketHeader = False
        
        # start the thread
        threading.Thread.__init__(self)
        self.name            = 'SnifferThread'
        self.start()
    
    def run(self):
        time.sleep(1) # let the banners print
        while True:
            try:
                with open(self.PIPE_SNIFFER, 'rb') as sniffer:
                    while True:
                        b = ord(sniffer.read(1))
                        self._newByte(b)
            except (IOError):
                print "WARNING: Could not read from pipe at \"{0}\".".format(
                    self.PIPE_SNIFFER
                )
                print "Is SiteAnalyzerAdapter running?"
                time.sleep(1)
    
    #======================== public ==========================================
    
    #======================== private =========================================
    
    def _newByte(self,b):
        '''
        Just received a byte from the sniffer
        '''
        with self.dataLock:
            self.rxBuffer += [b]
            
            # global header
            if   not self.doneReceivingGlobalHeader:
                if len(self.rxBuffer)==self.PCAP_GLOBALHEADER_LEN:
                    self.doneReceivingGlobalHeader    = True
                    self.rxBuffer                     = []
            
            # packet header
            elif not self.doneReceivingPacketHeader:
                if len(self.rxBuffer)==self.PCAP_PACKETHEADER_LEN:
                    self.doneReceivingPacketHeader    = True
                    self.packetHeader                 = self._parsePcapPacketHeader(self.rxBuffer)
                    assert self.packetHeader['incl_len']==self.packetHeader['orig_len']
                    self.rxBuffer                     = []
            
            # packet data
            else:
                if len(self.rxBuffer)==self.packetHeader['incl_len']:
                    self.doneReceivingPacketHeader    = False
                    self._newFrame(self.rxBuffer)
                    self.rxBuffer                     = []
    
    def _parsePcapPacketHeader(self,header):
        '''
        Parse a PCAP packet header
        
        Per https://wiki.wireshark.org/Development/LibpcapFileFormat:
        
        typedef struct pcaprec_hdr_s {
            guint32 ts_sec;         /* timestamp seconds */
            guint32 ts_usec;        /* timestamp microseconds */
            guint32 incl_len;       /* number of octets of packet saved in file */
            guint32 orig_len;       /* actual length of packet */
        } pcaprec_hdr_t;
        '''
        
        assert len(header)==self.PCAP_PACKETHEADER_LEN
        
        returnVal = {}
        (
            returnVal['ts_sec'],
            returnVal['ts_usec'],
            returnVal['incl_len'],
            returnVal['orig_len'],
        ) = struct.unpack('<IIII', ''.join([chr(b) for b in header]))
        
        return returnVal
    
    def _newFrame(self,frame):
        '''
        Just received a full frame from the sniffer
        '''
        
        # transform frame
        frame = self._transformFrame(frame)
        
        # publish frame
        self.publishThread.publishFrame(frame)
    
    def _transformFrame(self,frame):
        '''
        Replace BeamLogic header by ZEP header.
        '''
        
        beamlogic  = self._parseBeamlogicHeader(frame[1:1+self.BEAMLOGIC_HEADER_LEN])
        ieee154    = frame[self.BEAMLOGIC_HEADER_LEN+2:]
        ieee154[0] = ieee154[0] | 0x40 # fixing PAN ID compression bit (temporary)
        zep        = self._formatZep(
            channel     = beamlogic['Channel'],
            timestamp   = beamlogic['TimeStamp'],
            length      = len(ieee154),
        )
        
        return zep+ieee154
    
    def _parseBeamlogicHeader(self,header):
        '''
        Parse a Beamlogic packet header
        
        uint64    TimeStamp
        uint8     Channel
        uint8     RSSI
        uint32    GpsLat
        uint32    GpsLong
        '''
        
        assert len(header)==self.BEAMLOGIC_HEADER_LEN
        
        returnVal = {}
        (
            returnVal['TimeStamp'],
            returnVal['Channel'],
            returnVal['RSSI'],
            returnVal['GpsLat'],
            returnVal['GpsLong'],
        ) = struct.unpack('<QBBII', ''.join([chr(b) for b in header]))
        
        return returnVal
    
    def _formatZep(self,channel,timestamp,length):
        return [
            0x45,0x58,
            0x02,
            0x01,
            channel,
            0x00,0x01,
            0x01,
            0xff,
        ]+ \
        [ord(b) for b in struct.pack('>Q',timestamp)]+ \
        [
            0x02,0x02,0x02,0x02,
            0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,
            length,
        ]

class CliThread(object):
    def __init__(self):
        print 'ArgusProbe (BeamLogic device) {0}.{1}.{2}.{3} - (c) OpenWSN project'.format(
            ArgusVersion.VERSION[0],
            ArgusVersion.VERSION[1],
            ArgusVersion.VERSION[2],
            ArgusVersion.VERSION[3],
        )
        
        while True:
            input = raw_input('>')
            print input,

def main():
    # parse parameters
    
    # start thread
    publishThread  = PublishThread()
    snifferThread  = SnifferThread(publishThread)
    cliThread      = CliThread()

#============================ main ============================================

if __name__=="__main__":
    main()
