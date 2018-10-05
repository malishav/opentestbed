import paho.mqtt.client as mqtt
import threading
import time
import json
import requests
import socket
import traceback
import subprocess
import datetime
import serial
import os
import re
import Queue
#from PIL import Image
#from PIL import ImageFont
#from PIL import ImageDraw
#import pygame
import base64
#============================ defines =========================================

DEVICETYPE_BOX     = 'box'
DEVICETYPE_MOTE    = 'mote'
DEVICETYPE_ALL     = [
    DEVICETYPE_BOX,
    DEVICETYPE_MOTE
]

BAUDRATE = 500000

BROKER_ADDRESS     = "broker.mqttdashboard.com"
OTBOX_VERSION      = "1.1.0"
MOTE_USB_DEVICES   = [
    '/dev/ttyUSB1',
    '/dev/ttyUSB3',
    '/dev/ttyUSB5',
    '/dev/ttyUSB7',
]

IMAGE_THREAD_NAME       = 'image_thread'
HEARTBEAT_THREAD_NAME   = 'heartbeat_thread'
#============================ classes =========================================

class OtBox(object):

    HEARTBEAT_PERIOD              = 10
    PREFIX_CMD_HANDLER_NAME       = '_mqtt_handler_'
    OTBUX_SWTORUN_FILENAME        = 'otswtoload.json'
    LOCATION_FILE_NAME            = 'location.txt'
    FIRMWARE_EUI64_RETRIEVAL      = '{0}{1}'.format(os.getcwd(),'/A8/01bsp_eui64_prog')
    FIRMWARE_TEMP                 = '{0}{1}'.format(os.getcwd(),'/A8/03oos_openwsn_prog_19e849f')
    NEW_SOFTWARE_FILE_NAME        = 'new_software.zip'
    PICTURE_FILENAME              = 'picture'
    INIT_PICTURE_URL              = 'https://upload.wikimedia.org/wikipedia/commons/7/74/Openwsn_logo.png'

    def __init__(self):

        # store params

        # local variables
        self.OTBOX_ID                       = socket.gethostname()
        self.mqttopic_testbed_prefix        = 'opentestbed/deviceType/'
        self.mqtttopic_box_prefix           = 'opentestbed/deviceType/box/deviceId/{0}'.format(self.OTBOX_ID)
        self.mqtttopic_box_cmd_prefix       = '{0}/cmd'.format(self.mqtttopic_box_prefix)
        self.mqtttopic_box_notif_prefix     = '{0}/notif'.format(self.mqtttopic_box_prefix)
        self.mqtttopic_mote_prefix          = 'opentestbed/deviceType/mote/deviceId/'
        self.mqttconnected                  = False
        self.SerialRxBytePublishers         = {}
        self.SerialportHandlers             = {}
        self.start_time                     = time.time()
        self.change_image_queue             = Queue.Queue()
        self.motesinfo                      = []
        self.serialports_available          = self._discover_serialports_availables()
        try:
            with file('../{0}'.format(self.LOCATION_FILE_NAME),'r') as f:
                self.location               = f.read()
        except:
            self.location                   = 'not available'

        # connect to MQTT
        self.mqttclient                = mqtt.Client(self.OTBOX_ID)
        self.mqttclient.on_connect     = self._on_mqtt_connect
        self.mqttclient.on_message     = self._on_mqtt_message
        self.mqttclient.connect(BROKER_ADDRESS)

        # create serialport handlers and publishers
        for serialport in MOTE_USB_DEVICES:
            self.SerialportHandlers[serialport]       = SerialportHandler(serialport)
            self.SerialRxBytePublishers[serialport]   = SerialRxBytePublisher(
                                    rxqueue           = self.SerialportHandlers[serialport].rxqueue,
                                    serialport        = serialport,
                                    mqttclient        = self.mqttclient,
                                    mqtttopic         = None
            )

        # start mqtt client
        self.mqttthread                = threading.Thread(
            name                       = 'mqtt_loop_thread',
            target                     = self.mqttclient.loop_forever
        )
        self.mqttthread.start()

    #======================== public ==========================================

    #======================== private =========================================

    #=== top-level MQTT dispatching

    def _on_mqtt_connect(self, client, userdata, flags, rc):

        # remember I'm now connected
        self.mqttconnected   = True

        # subscribe to box commands
        client.subscribe('{0}/#'.format(self.mqtttopic_box_cmd_prefix))
        client.subscribe('opentestbed/deviceType/box/deviceId/all/cmd/#')

        # start heartbeat thread
        currentThreads  = self._getThreadsName()

        if HEARTBEAT_THREAD_NAME not in currentThreads:
            self.heartbeatthread = threading.Thread(
                name    = HEARTBEAT_THREAD_NAME,
                target  = self._heartbeatthread_func,
            )
            self.heartbeatthread.start()

        payload_status = {
            'token': 123
        }
        #client.publish(
        #    topic = 'opentestbed/deviceType/box/deviceId/all/cmd/discovermotes',
        #    payload = json.dumps(payload_status)
        #)
        self._mqtt_handler_discovermotes('box', 'all', json.dumps(payload_status))

        #if IMAGE_THREAD_NAME not in currentThreads:
        #    self.image_thread = threading.Thread(
        #        name    = IMAGE_THREAD_NAME,
        #        target  = self._display_image,
        #    )
        #    self.image_thread.start()
        #    self._excecute_commands('{0}/{1}'.format(self.mqtttopic_box_cmd_prefix, 'picturetoscreen'), json.dumps({'token': 0, 'url': self.INIT_PICTURE_URL}))


    def _on_mqtt_message(self, client, userdata, message):

        # call the handler
        self._excecute_commands(message.topic, message.payload)

    def _excecute_commands(self, topic, payload):
        # parse the topic to extract deviceType, deviceId and cmd ([0-9\-]+)
        try:
            m = re.search('opentestbed/deviceType/([a-z]+)/deviceId/([\w,\-]+)/cmd/([a-z]+)', topic)
            deviceType  = m.group(1)
            deviceId    = m.group(2)
            cmd         = m.group(3)
            
            print("Executing command: " + cmd)

            # verify params
            assert deviceType in DEVICETYPE_ALL
            device_to_comand      = []
            commands_handlers     = []
            if deviceId=='all':
                if deviceType == DEVICETYPE_MOTE:
                     for e in self.motesinfo:
                         if 'EUI64' in e:
                             device_to_comand    += [e['EUI64'],]
                else:
                    device_to_comand   = [self.OTBOX_ID,]
            else:
                device_to_comand      += [deviceId,]

            for d in device_to_comand:
                commands_handlers     += [threading.Thread(
                                name   = '{0}_command_{1}'.format(d, cmd),
                                target = self._excecute_command_safely,
                                args   = (deviceType, d, payload, cmd))
                                            ]
            for handler in commands_handlers:
                handler.start()
        except:
            pass

    def _excecute_command_safely(self, deviceType, deviceId, payload, cmd):
        '''
        Executes the handler of a command in a try/except environment so exception doesn't crash server.
        '''
        print("Executing command: " + cmd)
        returnVal       = {}
        try:
            # find the handler
            cmd_handler = getattr(self, '{0}{1}'.format(self.PREFIX_CMD_HANDLER_NAME, cmd))

            # call the handler
            returnVal['returnVal'] =  cmd_handler(deviceType, deviceId, payload)

        except Exception as err:
            returnVal = {
                'success':     False,
                'exception':   str(type(err)),
                'traceback':   traceback.format_exc(),
            }
        else:
            returnVal['success']  = True
        finally:
            try:
                returnVal['token']    = json.loads(payload)['token']
            except:
                pass

            self.mqttclient.publish(
                topic   = '{0}{1}/deviceId/{2}/resp/{3}'.format(self.mqttopic_testbed_prefix,deviceType,deviceId,cmd),
                payload = json.dumps(returnVal),
            )

    #=== command handlers

    # box

    def _mqtt_handler_echo(self, deviceType, deviceId, payload):
        '''
        opentestbed/deviceType/box/deviceId/box1/cmd/echo
        '''
        assert deviceType==DEVICETYPE_BOX

        return json.loads(payload)

    def _mqtt_handler_status(self, deviceType, deviceId, payload):
        '''
        opentestbed/deviceType/box/deviceId/box1/cmd/status
        '''
        assert deviceType==DEVICETYPE_BOX

        returnVal       = {
            'software_version':   OTBOX_VERSION,
            'currenttime':        time.ctime(),
            'starttime':          time.ctime(self.start_time),
            'uptime':             '{0}'.format(datetime.timedelta(seconds=(time.time()-self.start_time))),
            'motes':              self.motesinfo,
            'IP_address':         subprocess.check_output(["hostname",""]).rstrip(),
            'location':           self.location,
        }

        #with file(self.OTBUX_SWTORUN_FILENAME,'r') as f:
        #    update_info = f.read()
        #returnVal['last_changesoftware_succesful']    = json.loads(update_info)['last_changesoftware_succesful']
        returnVal['threads_name']       = self._getThreadsName()

        return returnVal

    def _mqtt_handler_discovermotes(self, deviceType, deviceId, payload):
        '''
        opentestbed/deviceType/box/deviceId/box1/cmd/discovermotes
        '''
        print("Motes discovery started")
        assert deviceType==DEVICETYPE_BOX

        # turn off serial_reader if exist
        for serialport in MOTE_USB_DEVICES:
            self.SerialportHandlers[serialport].disconnectSerialPort()

        # discover serialports available
        self.serialports_available          = self._discover_serialports_availables()

        # bootload EUI64 retrieval firmware on all motes
        bootload_successes = self._bootload_motes(
            serialports           =self.serialports_available,
            firmware_file         = self.FIRMWARE_EUI64_RETRIEVAL,
        )
        for (idx,e) in enumerate(self.motesinfo):
            e['firmware_description'] = 'FIRMWARE_EUI64_RETRIEVAL'
            e['bootload_success']     = bootload_successes[idx]
        

        # get EUI64 from serials ports for motes with bootload_success = True
        for e in self.motesinfo:
            print ("Bootload status: " + str(e['bootload_success']))
            if e['bootload_success']==True:
                print("EUI64 retrieval firmware flashed successfully")
                ser     = serial.Serial(e['serialport'],baudrate=BAUDRATE)
                while True:
                    line  = ser.readline()
                    if len(line.split("-")) == 8 and len(line) == 25:
                        e['EUI64'] = line[:len(line)-2]

                        self._bootload_motes(
                            serialports = self.serialports_available,
                            firmware_file = self.FIRMWARE_TEMP
                        )

                        break

        for e in self.motesinfo:
            if 'EUI64' in e:
                # subscribe to the topics of each mote
                self.mqttclient.subscribe('{0}{1}/cmd/#'.format(self.mqtttopic_mote_prefix, e['EUI64']))
                # set topic of SerialRxBytePublishers
                self.SerialRxBytePublishers[e['serialport']].mqtttopic    = '{0}{1}/notif/frommoteserialbytes'.format(self.mqtttopic_mote_prefix,e['EUI64'])
                # start reading serial port
                self.SerialportHandlers[e['serialport']].connectSerialPort()


        self.mqttclient.subscribe('{0}{1}/cmd/#'.format(self.mqtttopic_mote_prefix, 'all'))

        return {
            'motes': self.motesinfo
        }

    def _mqtt_handler_changesoftware(self, deviceType, deviceId, payload):
        '''
        opentestbed/deviceType/box/deviceId/box1/cmd/changesoftware
        '''
        assert deviceType==DEVICETYPE_BOX

        r     = requests.get(json.loads(payload)['url'])
        open(self.NEW_SOFTWARE_FILE_NAME, 'wb').write(r.content)
        subprocess.call(['unzip', '-o', self.NEW_SOFTWARE_FILE_NAME])
        subprocess.call('mv opentestbed* new_software', shell=True)
        subprocess.call('cp new_software/install/* ../', shell=True )

        # remember the URL to run
        with file(self.OTBUX_SWTORUN_FILENAME,'w') as f:
            f.write(payload)

        # reboot the computer this program runs on
        reboot_function_thread    = threading.Thread(
            name                  = 'reboot_thread',
            target                = self._reboot_function
        )
        reboot_function_thread.start()

        return {}

    
    #def _mqtt_handler_picturetoscreen(self, deviceType, deviceId, payload):
        '''
        #opentestbed/deviceType/box/deviceId/box1/cmd/picturetoscreen
        '''

    #    assert deviceType==DEVICETYPE_BOX
    #    image = Image.open(requests.get(json.loads(payload)['url'], stream=True).raw)
    #    image.thumbnail((480,320),Image.ANTIALIAS)
    #    self.change_image_queue.put(image)
    #    self.change_image_queue.join()
    #    return {}

    #def _mqtt_handler_colortoscreen(self, deviceType, deviceId, payload):
        '''
        #opentestbed/deviceType/box/deviceId/box1/cmd/colortoscreen
        '''
    #    assert deviceType==DEVICETYPE_BOX
    #    payload    = json.loads(payload)
    #    self.change_image_queue.put(Image.new('RGB', (480,320), (payload['r'],payload['g'],payload['b'])))
    #    self.change_image_queue.join()

    #def _mqtt_handler_hostnametoscreen(self, deviceType, deviceId, payload):
        '''
        #opentestbed/deviceType/box/deviceId/box1/cmd/colortoscreen
        '''
    #    image_to_display  = Image.new('RGB', (480,320), (255,255,0))
    #    font = ImageFont.truetype("/usr/share/fonts/truetype/freefont/FreeMono.ttf", 80)
    #    ImageDraw.Draw(image_to_display).text((0, 0),self.OTBOX_ID,(0,0,0), font=font)
    #    self.change_image_queue.put(image_to_display)
    #    self.change_image_queue.join() '''

    def _mqtt_handler_changelocation(self, deviceType, deviceId, payload):
        '''
        opentestbed/deviceType/box/deviceId/box1/cmd/changelocation
        '''
        assert deviceType==DEVICETYPE_BOX
        self.location   = json.loads(payload)['location']
        with file('../{0}'.format(self.LOCATION_FILE_NAME),'w') as f:
            f.write(self.location)

    # motes

    def _mqtt_handler_program(self, deviceType, deviceId, payload):
        '''
        opentestbed/deviceType/mote/deviceId/01-02-03-04-05-06-07-08/cmd/program
        '''
        assert deviceType==DEVICETYPE_MOTE
        assert deviceId!='all'

        payload    = json.loads(payload) # shorthand
        mote       = self._eui64_to_moteinfoelem(deviceId)
        
        # disconnect from the serialports
        self.SerialportHandlers[mote['serialport']].disconnectSerialPort()
        time.sleep(2) # wait 2 seconds to release the serial ports
        
        # store the firmware to load into a temporary file
        with open(self.FIRMWARE_TEMP,'w') as f:
            if 'url' in payload: # download file from url if present
                file   = requests.get(payload['url'], allow_redirects=True)
                f.write(file.content)
            elif 'hex' in payload: # export hex file received if present
                f.write(base64.b64decode(payload['hex']))
            else:
                assert "The supported keys {0}, {1} are not in the payload. ".format('url','hex')


        # bootload the mote
        bootload_success = self._bootload_motes(
            serialports      = [mote['serialport']],
            firmware_file    = self.FIRMWARE_TEMP,
        )

        assert len(bootload_success)==1

        # record success of bootload process
        mote['bootload_success']       = bootload_success[0]
        mote['firmware_description']   = payload['description']

        assert mote['bootload_success'] ==True
        self.SerialportHandlers[mote['serialport']].connectSerialPort()
        print 'started'

    def _mqtt_handler_tomoteserialbytes(self, deviceType, deviceId, payload):
        '''
        opentestbed/deviceType/mote/deviceId/01-02-03-04-05-06-07-08/cmd/tomoteserialbytes
        '''
        assert deviceType==DEVICETYPE_MOTE
        payload    = json.loads(payload)
        mote       = self._eui64_to_moteinfoelem(deviceId)
        serialHandler = serial.Serial(mote['serialport'], baudrate=BAUDRATE)
        serialHandler.write(bytearray(payload['serialbytes']))
        self.SerialportHandlers[mote['serialport']].connectSerialPort()

    def _mqtt_handler_reset(self, deviceType, deviceId, payload):
        '''
        opentestbed/deviceType/mote/deviceId/01-02-03-04-05-06-07-08/cmd/reset
        '''
        assert deviceType==DEVICETYPE_MOTE

        mote            = self._eui64_to_moteinfoelem(deviceId)
        self.SerialportHandlers[mote['serialport']].disconnectSerialPort()
        pyserialHandler = serial.Serial(mote['serialport'], baudrate=BAUDRATE)
        pyserialHandler.setDTR(False)
        pyserialHandler.setRTS(True)
        time.sleep(0.2)
        pyserialHandler.setDTR(True)
        pyserialHandler.setRTS(False)
        time.sleep(0.2)
        pyserialHandler.setDTR(False)

        ## start serial
        self.SerialportHandlers[mote['serialport']].connectSerialPort()


    def _mqtt_handler_disable(self, deviceType, deviceId, payload):
        '''
        opentestbed/deviceType/mote/deviceId/01-02-03-04-05-06-07-08/cmd/disable
        '''
        assert deviceType==DEVICETYPE_MOTE

        payload    = json.loads(payload) # shorthand
        mote       = self._eui64_to_moteinfoelem(deviceId)
        # off serial
        self.SerialportHandlers[mote['serialport']].disconnectSerialPort()
        bootload_success     = self._bootload_motes(
            serialports      = [mote['serialport']],
            firmware_file    = self.FIRMWARE_EUI64_RETRIEVAL,
        )
        mote['bootload_success']       = bootload_success[0]
        mote['firmware_description']   = 'FIRMWARE_EUI64_RETRIEVAL'
        assert mote['bootload_success']==True

    #=== heartbeat

    def _heartbeatthread_func(self):
        while True:
            # wait a bit
            time.sleep(self.HEARTBEAT_PERIOD)
            # publish a heartbeat message
            self.mqttclient.publish(
                topic   = '{0}/heartbeat'.format(self.mqtttopic_box_notif_prefix),
                payload = json.dumps({'software_version': OTBOX_VERSION}),
            )

    #=== helpers

    # bootload

    def _bootload_motes(self, serialports, firmware_file):
        '''
        bootloads firmware_file onto multiple motes in parallel
        '''
        print("Bootloading firmware file: " + firmware_file )
        # start bootloading each mote
        ongoing_bootloads    = {}
        for serialport in serialports:
        
            # simply the name
            port = serialport.split('/')[-1]
        
            # stop serial reader
            #ongoing_bootloads[port] = subprocess.Popen(['python', 'bootloaders/cc2538-bsl.py', '-e', '--bootloader-invert-lines', '-w', '-b', '400000', '-p', serialport, firmware_file], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            ongoing_bootloads[port] = subprocess.Popen(['flash_a8_m3', firmware_file])

        returnVal = []
        for ongoing_bootload in ongoing_bootloads:
            # wait for this bootload process to finish
            (stdout, stderr) = ongoing_bootloads[ongoing_bootload].communicate()
            
            # record the last output of bootload process
            with open("log_{0}.txt".format(ongoing_bootload),'w') as f:
                f.write("stdout: {0} stderr {1}".format(stdout,stderr))

            # record whether bootload worked or not
            returnVal += [ongoing_bootloads[ongoing_bootload].returncode == 0]

        return returnVal

    # misc
    def _eui64_to_moteinfoelem(self, eui64):
        returnVal = None
        for m in self.motesinfo:
            if 'EUI64' in m:
                if m['EUI64']==eui64:
                    assert returnVal==None
                    returnVal = m
                    break
        assert returnVal!=None
        return returnVal

    def _reboot_function(self):
        time.sleep(3)
        subprocess.call(["sudo","reboot"])

    #def _display_image(self):
    #    pygame.init()
    #    size       = (pygame.display.Info().current_w, pygame.display.Info().current_h)
    #    black      = 0, 0, 0
    #    screen     = pygame.display.set_mode(size,pygame.FULLSCREEN)
    #    while True:
    #        if self.change_image_queue.empty()==False:
    #            picture      = self.change_image_queue.get()
    #            image        = pygame.image.fromstring(picture.tobytes(), picture.size, picture.mode)
    #            imagerect    = image.get_rect()
    #            screen.fill(black)
    #            screen.blit(image, (240-picture.size[0]/2,160-picture.size[1]/2))
    #            pygame.display.flip()
    #            self.change_image_queue.task_done()
    #        time.sleep(0.2)

    def _discover_serialports_availables(self):
        serialports_available     = []
        self.motesinfo            = []
        for serialport in MOTE_USB_DEVICES:
            try:
                ser     = serial.Serial(serialport)
                serialports_available  += [serialport,]
                ser.close()
            except:
                pass

        self.motesinfo  = [
            {
                'serialport': i,
            } for i in serialports_available
        ]
        return serialports_available

    def _getThreadsName(self):
        threadsName     = []
        for t in threading.enumerate():
            threadsName.append(t.getName())
        return threadsName

class SerialRxBytePublisher(threading.Thread):

    PUBLICATION_PERIOD = 1

    def __init__(self,rxqueue,serialport,mqttclient,mqtttopic):

        # store params
        self.rxqueue    = rxqueue
        self.mqttclient = mqttclient
        self.mqtttopic  = mqtttopic

        # local variables
        self.goOn       = True

        # initialize thread
        threading.Thread.__init__(self)
        self.name       = 'SerialRxBytePublisher@{0}'.format(serialport)
        self.start()

    def run(self):
        while self.goOn:

            # wait
            time.sleep(self.PUBLICATION_PERIOD)
            try:
                # read queue
                buffer_to_send    = []
                while not self.rxqueue.empty():
                    temp_reading  = self.rxqueue.get()
                    for i in temp_reading:
                        buffer_to_send += [ord(i)]
                # publish
                if buffer_to_send:
                    payload = {
                        'serialbytes': buffer_to_send,
                    }
                    self.mqttclient.publish(
                        topic   = self.mqtttopic,
                        payload = json.dumps(payload),
                    )
            except:
                pass

    #======================== public ==========================================

    def close(self):
        self.goOn = False

class SerialportHandler(threading.Thread):
    '''
    Connects to serial port. Puts received serial bytes in queue. Method to send bytes.
    One per mote.
    Can be started/stopped many times (used when reprogramming).
    '''
    def __init__(self, serialport):

        # store params
        self.serialport           = serialport

        # local variables
        self.rxqueue              = Queue.Queue()
        self.serialHandler        = None
        self.goOn                 = True
        self.pleaseConnect        = False
        self.dataLock             = threading.RLock()

        # initialize thread
        super(SerialportHandler, self).__init__()
        self.name                 = 'SerialportHandler@{0}'.format(self.serialport)
        self.start()

    def run(self):
        while self.goOn:

            try:

                with self.dataLock:
                    pleaseConnect = self.pleaseConnect

                if pleaseConnect:

                    # open serial port
                    self.serialHandler = serial.Serial(self.serialport, baudrate=BAUDRATE)

                    # read byte
                    while True:
                        waitingbytes   = self.serialHandler.inWaiting()
                        if waitingbytes != 0:
                            c = self.serialHandler.read(waitingbytes) # blocking
                            self.rxqueue.put(c)
                            time.sleep(0.1)

            except:
                # mote disconnected, or pyserialHandler closed
                # destroy pyserial instance
                self.serialHandler = None

            # wait
            time.sleep(1)

    #======================== public ==========================================

    def connectSerialPort(self):
        with self.dataLock:
            self.pleaseConnect = True

    def disconnectSerialPort(self):
        with self.dataLock:
            self.pleaseConnect = False
        try:
            self.serialHandler.close()
        except:
            pass

    def close(self):
        self.goOn            = False

    #======================== private =========================================

#============================ main ============================================

if __name__ == '__main__':
    otbox = OtBox()
