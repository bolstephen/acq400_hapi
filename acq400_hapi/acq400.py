#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
acq400.py interface to one acq400 appliance instance

- enumerates all site services, available as uut.sX.knob
- simple property interface allows natural "script-like" usage
 - eg::

       uut1.s0.set_arm = 1

 - equivalent to running this on a logged in shell session on the UUT::

       set.site1 set_arm=1

 - monitors transient status on uut, provides blocking events
 - read_channels() - reads all data from channel data service.
  Created on Sun Jan  8 12:36:38 2017

  @author: pgm
"""

import threading
import re

import os
import errno
import signal
import sys
if __name__ == '__main__':
    import netclient
else:
    from . import netclient

import numpy as np
import socket
import timeit
import time

class AcqPorts:
    """server port constants"""
    TSTAT = 2235
    STREAM = 4210
    SITE0 = 4220
    SEGSW = 4250
    SEGSR = 4251
    DPGSTL = 4521
    GPGSTL= 4541
    GPGDUMP = 4543

    WRPG = 4606

    DIO482_PG_STL = 45001  
    DIO482_PG_DUMP = DIO482_PG_STL+2
    
    BOLO8_CAL = 45072
    DATA0 = 53000
    DATAT = 53333
    MULTI_EVENT_TMP = 53555
    MULTI_EVENT_DISK = 53556
    DATA_SPY = 53667
    LIVETOP = 53998
    ONESHOT = 53999
    AWG_ONCE = 54201
    AWG_AUTOREARM = 54202
    AWG_CONTINUOUS = 54205
    MGTDRAM = 53990
    MGTDRAM_PULL_DATA = 53991

class AcqSites:
    # site service at AcqPorts.SITE0+ AcqSites.SITEi
    SITE0 = 0
    SITE1 = 1
    SITE2 = 2
    SITE3 = 3
    SITE4 = 4
    SITE5 = 5
    SITE6 = 6
    SITE_CA = 13
    SITE_CB = 12
    SITE_CC = 11
    SITE_DSP = 14

class SF:
    """state constants"""
    STATE = 0
    PRE = 1
    POST = 2
    ELAPSED = 3
    DEMUX = 5

class STATE:
    """transient states"""
    IDLE = 0
    ARM = 1
    RUNPRE = 2
    RUNPOST = 3
    POPROCESS = 4
    CLEANUP = 5
    @staticmethod
    def str(st):
        if st==STATE.IDLE:
            return "IDLE"
        if st==STATE.ARM:
            return "ARM"
        if st==STATE.RUNPRE:
            return "RUNPRE"
        if st==STATE.RUNPOST:
            return "RUNPOST"
        if st==STATE.POPROCESS:
            return "POPROCESS"
        if st==STATE.CLEANUP:
            return "CLEANUP"
        return "UNDEF"

class Signals:
    EXT_TRG_DX = 'd0'
    INT_TRG_DX = 'd1'
    MB_CLK_DX = 'd1'

class StreamClient(netclient.Netclient):
    """handles live streaming data"""
    def __init__(self, addr):
        print("worktodo")

class RawClient(netclient.Netclient):
    """ handles raw data from any service port
    """
    def __init__(self, addr, port):
        netclient.Netclient.__init__(self, addr, port)

    def read(self, nelems, data_size=2, ncols=1, maxbuf=0x400000):
        """read ndata from channel data server, return as np array.
        Args:
            nelems number of data elements, each data_size*ncols
            nelems <=0 :: read until the end

            data_size : 2|4 short or int

            ncols : optional, to create a 2D array
        """
        _dtype = np.dtype('i4' if data_size == 4 else 'i2')   # hmm, what if unsigned?
        if nelems <= 0:
            nelems = 0x80000000             #2GB approximates infinity. what is infinity in python?

        bytestogo = nelems * data_size * ncols
        total_buf = ""

        while bytestogo > 0:
            new_buf = self.sock.recv(bytestogo)
            if not new_buf:
                break               # end of file
            bytestogo = bytestogo - len(new_buf)
            total_buf += new_buf    # still dubious of append :-)

        return np.frombuffer(total_buf, _dtype)

    def get_blocks(self, nelems, data_size=2, ncols=1):
        block = np.array([1])
        while len(block) > 0:
            block = self.read(nelems, data_size=data_size, ncols=ncols)
            if len(block) > 0:
                yield block


class MgtDramPullClient(RawClient):
    def __init__(self, addr):
        RawClient.__init__(self, addr, AcqPorts.MGTDRAM_PULL_DATA)



class ChannelClient(netclient.Netclient):
    """handles post shot data for one channel.

    Args:
        addr (str) : ip address or dns name

        ch (int) : channel number 1..N

    """
    def __init__(self, addr, ch):
        netclient.Netclient.__init__(self, addr, AcqPorts.DATA0+ch)

# on Linux, recv returns on ~mtu
# on Windows, it may buffer up, and it's very slow unless we use a larger buffer
    def read(self, ndata, data_size=2, maxbuf=0x400000):
        """read ndata from channel data server, return as np array.
        Args:
            ndata (int): number of elements

            data_size : 2|4 short or int

            maxbuf=4096 : max bytes to read per packet

        Returns:
            np: data array

        * TODO buffer +=

         this is probably horribly inefficient probably better::

          retbuf = np.array(dtype, ndata)
          retbuf[cursor].

        """
        _dtypes = { 1 : 'i1', 2: 'i2', 4: 'i4' }
        _dtype = np.dtype(_dtypes[data_size])
        total_buffer = buffer = self.sock.recv(maxbuf)

        if int(ndata) == 0 or int(ndata) == -1:
            while True:
                buffer = self.sock.recv(maxbuf)
                if not buffer:
                    return np.frombuffer(total_buffer, dtype=_dtype, count=-1)
                total_buffer += buffer

        while len(buffer) < ndata*data_size:
            buffer += self.sock.recv(maxbuf)

        return np.frombuffer(buffer, dtype=_dtype, count=ndata)


class ExitCommand(Exception):
    pass


def signal_handler(signal, frame):
    raise ExitCommand()

class Statusmonitor:
    """ monitors the status channel

    Efficient event-driven monitoring in a separate thread
    """
    st_re = re.compile(r"([0-9]) ([0-9]+) ([0-9]+) ([0-9]+) ([0-9])+" )

    def __repr__(self):
        return repr(self.logclient)
    def st_monitor(self):
        while self.quit_requested == False:
            st = self.logclient.poll()
            match = self.st_re.search(st)
            # status is a match. need to look at group(0). It's NOT a LIST!
            if match:
                statuss = match.groups()
                status1 = [int(x) for x in statuss]
                if self.trace > 1:
                    print("%s <%s" % (repr(self), status1))
                if self.status != None:
#                    print("Status check %s %s" % (self.status0[0], status[0]))
                    if self.status[SF.STATE] != 0 and status1[SF.STATE] == 0:
                        if self.trace:
                            print("%s STOPPED!" % (self.uut))
                        self.stopped.set()
                        self.armed.clear()
#                print("status[0] is %d" % (status[0]))
                    if status1[SF.STATE] == 1:
                        if self.trace:
                            print("%s ARMED!" % (self.uut))
                        self.armed.set()
                        self.stopped.clear()
                    if self.status[SF.STATE] == 0 and status1[SF.STATE] > 1:
                        if self.trace:
                            print("ERROR: %s skipped ARM %d -> %d" % (self.uut, self.status[0], status1[0]))
                        self.quit_requested = True
                        os.kill(self.main_pid, signal.SIGINT)
                        sys.exit(1)
                self.status = status1
            elif self.trace > 1:
                print("%s <%s>" % (repr(self), st))

    def get_state(self):
        return self.status[SF.STATE]

    def wait_event(self, ev, descr):
    #       print("wait_%s 02 %d" % (descr, ev.is_set()))
        while ev.wait(0.1) == False:
            if self.quit_requested:
                print("QUIT REQUEST call exit %s" % (descr))
                sys.exit(1)

#        print("wait_%s 88 %d" % (descr, ev.is_set()))
        ev.clear()
#        print("wait_%s 99 %d" % (descr, ev.is_set()))

    def wait_armed(self):
        """
        blocks until uut is ARMED
        """
        self.wait_event(self.armed, "armed")

    def wait_stopped(self):
        """
        blocks until uut is STOPPED
        """
        self.wait_event(self.stopped, "stopped")

    trace = int(os.getenv("STATUSMONITOR_TRACE", "0"))

    def __init__(self, _uut, _status):
        self.quit_requested = False
        self.trace = Statusmonitor.trace
        self.uut = _uut
        self.main_pid = os.getpid()
        self.status = _status
        self.stopped = threading.Event()
        self.armed = threading.Event()
        self.logclient = netclient.Logclient(_uut, AcqPorts.TSTAT)
        self.st_thread = threading.Thread(target=self.st_monitor)
        self.st_thread.setDaemon(True)
        self.st_thread.start()


class NullFilter:
    def __call__ (self, st):
        print(st)

null_filter = NullFilter()

class ProcessMonitor:
    st_re = re.compile(r"^END" )

    def st_monitor(self):
        while self.quit_requested == False:
            st = self.logclient.poll()
            self.output_filter(st)
            match = self.st_re.search(st)
            if match:
                self.quit_requested = True

    def __init__(self, _uut, _filter):
        self.quit_requested = False
        self.output_filter = _filter
        self.logclient = netclient.Logclient(_uut, AcqPorts.MGTDRAM)
        self.logclient.termex = re.compile("(\n)")
        self.st_thread = threading.Thread(target=self.st_monitor)
        self.st_thread.setDaemon(True)
        self.st_thread.start()

class Acq400:
    """
    host-side proxy for Acq400 uut.

    discovers and maintains all site servers
    maintains a monitor thread on the monitor port
    handles multiple channel post shot upload

    Args:
        _uut (str) : ip-address or dns name

        monitor=True (bool) : set false to stub monitor,
          useful for tracing on a second connection to an active system.
    """

    def init_site_client(self, site):
        svc = netclient.Siteclient(self.uut, AcqPorts.SITE0+site)
        self.svc["s%d" % site] = svc
        self.modules[site] = svc

        if self.awg_site == 0 and svc.module_name.startswith("ao"):
            self.awg_site = site
        self.mod_count += 1
        
    
    @classmethod
    def create_uuts(cls, uut_names):
        """ create_uuts():  factory .. create them in parallel

        *** Experimental Do Not Use ***

        """
        uuts = []
        uut_threads = {}
        for uname in uut_names:
            uut_threads[uname] = \
                    threading.Thread(\
                        target=lambda u, l: l.append(cls(u)), \
                        args=(uname, uuts))
        for uname in uut_names:
            uut_threads[uname].start()
        for t in uut_threads:
            uut_threads[t].join(10.0)

        return uuts


    uuts = {}
    
    def __init__(self, _uut, monitor=True):
        try:
            self.__dict__ = Acq400.uuts[_uut]
            return
        except KeyError:
            pass
        
        self.NL = re.compile(r"(\n)")
        self.uut = _uut
        self.trace = 0
        self.save_data = None
        self.svc = {}
        self.modules = {}
        self.mod_count = 0
        # channel index from 1,..
        self.cal_eslo = [0, ]
        self.cal_eoff = [0, ]
        self.mb_clk_min = 4000000

        s0 = self.svc["s0"] = netclient.Siteclient(self.uut, AcqPorts.SITE0)
        sl = s0.SITELIST.split(",")
        sl.pop(0)
        self.awg_site = 0
        site_enumerators = {}
        for sm in sl:
            site_enumerators[sm] = \
                    threading.Thread(target=self.init_site_client,\
                        args=(int(sm.split("=").pop(0)),)\
                    )
        for sm in sl:
            site_enumerators[sm].start()

        for sm in sl:
#            print("join {}".format(site_enumerators[sm]))
            site_enumerators[sm].join(10.0)
            
        self.sites = [int(s.split('=')[0]) for s in sl]

# init _status so that values are valid even if this Acq400 doesn't run a shot ..
        _status = [int(x) for x in s0.state.split(" ")]
        if monitor:
            self.statmon = Statusmonitor(self.uut, _status)
        Acq400.uuts[_uut] = self.__dict__


    def __getattr__(self, name):
        if self.svc.get(name) != None:
            return self.svc.get(name)
        else:
            msg = "'{0}' object has no attribute '{1}'"
            raise AttributeError(msg.format(type(self).__name__, name))

    def state(self):
        return self.statmon.status[SF.STATE]
    def post_samples(self):
        return self.statmon.status[SF.POST]
    def pre_samples(self):
        return self.statmon.status[SF.PRE]
    def elapsed_samples(self):
        return self.statmon.status[SF.ELAPSED]
    def demux_status(self):
        return self.statmon.status[SF.DEMUX]
    def samples(self):
        return self.pre_samples() + self.post_samples()

    def get_aggregator_sites(self):
        return self.s0.aggregator.split(' ')[1].split('=')[1].split(',')

    def fetch_all_calibration(self):
        print("Fetching calibration data")
        for m in (self.modules[int(c)] for c in self.get_aggregator_sites()):
            self.cal_eslo.extend(m.AI_CAL_ESLO.split(' ')[3:])
            self.cal_eoff.extend(m.AI_CAL_EOFF.split(' ')[3:])

    def scale_raw(self, raw, volts=False):
        for (sx, m) in list(self.modules.items()):
            if m.MODEL.startswith("ACQ43"):
                rshift = 8
            elif m.data32 == '1':
                # volts calibration is normalised to 24b
                if m.adc_18b == '1':
                    rshift = 14 - (8 if volts else 0)
                else:
                    rshift = 16 - (8 if volts else 0)
            else:
                rshift = 0
            break
        return np.right_shift(raw, rshift)

    def chan2volts(self, chan, raw):
        """ chan2volts(self, chan, raw) returns calibrated volts for channel

            Args:

               chan: 1..nchan

               raw:  raw bits to convert.

        """
        if len(self.cal_eslo) == 1:
            self.fetch_all_calibration()

        eslo = float(self.cal_eslo[chan])
        eoff = float(self.cal_eoff[chan])
        return np.add(np.multiply(raw, eslo), eoff)


    def read_chan(self, chan, nsam = 0, data_size = None):
        if chan != 0 and nsam == 0:
            nsam = self.pre_samples()+self.post_samples()
        if data_size == None:
            data_size = 4 if self.s0.data32 == '1' else 2

        cc = ChannelClient(self.uut, chan)
        ccraw = cc.read(nsam, data_size=data_size)

        if self.save_data:
            try:
                os.makedirs(self.save_data)
            except OSError as exception:
                if exception.errno != errno.EEXIST:
                    raise

            with open("%s/%s_CH%02d" % (self.save_data, self.uut, chan), 'wb') as fid:
                ccraw.tofile(fid, '')

        return ccraw

    def read_decims(self, nsam = 0):
        if nsam == 0:
            nsam = self.pre_samples()+self.post_samples()
        cc = ChannelClient(self.uut, AcqPorts.DATAT-AcqPorts.DATA0)
        ccraw = cc.read(nsam, data_size=1)

        if self.save_data:
            try:
                os.makedirs(self.save_data)
            except OSError as exception:
                if exception.errno != errno.EEXIST:
                    raise

            with open("%s/%s_DEC" % (self.save_data, self.uut, chan), 'wb') as fid:
                ccraw.tofile(fid, '')

        return ccraw


    def nchan(self):
        return int(self.s0.NCHAN)

    def read_channels(self, channels=(), nsam=0):
        """read all channels post shot data.

        Returns:
            chx (list) of np arrays.
        """


        if channels == ():
            channels = list(range(1, self.nchan()+1))
        elif type(channels) == int:
            channels = (channels,)

    #      print("channels {}".format(channels))

        chx = []
        data_size = 4 if self.s0.data32 == '1' else 2
        demux_state = int(self.s0.transient.split("DEMUX=",1)[1][0])
        if demux_state:
            for ch in channels:
                if self.trace:
                    print("%s CH%02d start.." % (self.uut, ch))
                    start = timeit.default_timer()

                chx.append(self.read_chan(ch, nsam, data_size=data_size))

                if self.trace:
                    tt = timeit.default_timer() - start
                    print("%s CH%02d complete.. %.3f s %.2f MB/s" %
                        (self.uut, ch, tt, len(chx[-1])*2/1000000/tt))
        else:
            data = np.array(self.read_chan(0, nsam, data_size=data_size))
            data = data.reshape((-1, int(self.s0.NCHAN)))
            data = data[:,np.array(channels)-1].transpose()
            chx = [ item for item in data ]

        return chx

    # DEPRECATED
    def load_segments(self, segs):
        with netclient.Netclient(self.uut, AcqPorts.SEGSW) as nc:
            for seg in segs:
                nc.sock.send((seg+"\n").encode())
    # DEPRECATED
    def show_segments(self):
        with netclient.Netclient(self.uut, AcqPorts.SEGSR) as nc:
            while True:
                buf = nc.sock.recv(1024)
                if buf:
                    print(buf)
                else:
                    break

    def clear_counters(self):
        for s in self.svc:
            self.svc[s].sr('*RESET=1')

    def set_sync_routing_master(self, clk_dx="d1", trg_dx="d0"):
        self.s0.SIG_SYNC_OUT_CLK = "CLK"
        self.s0.SIG_SYNC_OUT_CLK_DX = clk_dx
        self.s0.SIG_SYNC_OUT_TRG = "TRG"
        self.s0.SIG_SYNC_OUT_TRG_DX = trg_dx

    def set_sync_routing_slave(self):
        self.set_sync_routing_master()
        self.s0.SIG_SRC_CLK_1 = "HDMI"
        self.s0.SIG_SRC_TRG_0 = "HDMI"

    def set_sync_routing(self, role):
        # deprecated
        # set sync mode on HDMI daisychain
        # valid roles: master or slave
        if role == "master":
            self.set_sync_routing_master()
        elif role == "slave":
            self.set_sync_routing_slave()
        else:
            raise ValueError("undefined role {}".format(role))

    def set_mb_clk(self, hz=4000000, src="zclk", fin=1000000):
        hz = int(hz)
        if src == "zclk":
            self.s0.SIG_ZCLK_SRC = "INT33M"
            self.s0.SYS_CLK_FPMUX = "ZCLK"
            self.s0.SIG_CLK_MB_FIN = 33333000
        elif src == "xclk":
            self.s0.SYS_CLK_FPMUX = "XCLK"
            self.s0.SIG_CLK_MB_FIN = 32768000
        else:
            self.s0.SYS_CLK_FPMUX = "FPCLK"
            self.s0.SIG_CLK_MB_FIN = fin

        if hz >= self.mb_clk_min:
            self.s0.SIG_CLK_MB_SET = hz
            self.s1.CLKDIV = '1'
        else:
            for clkdiv in range(1,2000):
                if hz*clkdiv >= self.mb_clk_min:
                    self.s0.SIG_CLK_MB_SET = hz*clkdiv
                    self.s1.CLKDIV = clkdiv
                    return
            raise ValueError("frequency out of range {}".format(hz))

    def load_stl(self, stl, port, trace = False, wait_eof = True, wait_eol = True):
        termex = re.compile("\n")
        with netclient.Netclient(self.uut, port) as nc:
            lines = stl.split("\n")
            for ll in lines:
                if trace:
                    print("> {}".format(ll))
                if len(ll) < 2:
                    if trace:
                        print("skip blank")
                    continue
                if ll.startswith('#'):
                    if trace:
                        print("skip comment")
                    continue
                nc.sock.send((ll+"\n").encode())
                if wait_eol:
                    rx = nc.sock.recv(4096)
                    if trace:
                        print("< {}".format(rx))
            nc.sock.send("EOF\n".encode())
            nc.sock.shutdown(socket.SHUT_WR)
            while wait_eof:
                rx = nc.sock.recv(4096)
                if trace:
                    print("< {}".format(rx))
                if (str(rx).find("EOF")) != -1:
                    break
                wait_end = wait_eof



    def load_gpg(self, stl, trace = False):
            self.load_stl(stl, AcqPorts.GPGSTL, trace)

        
    def load_dpg(self, stl, trace = False):
        self.load_stl(stl, AcqPorts.DPGSTL, trace, wait_eol=False)

    def load_wrpg(self, stl, trace = False):
        self.load_stl(stl, AcqPorts.WRPG, trace)

    class AwgBusyError(Exception):
        def __init__(self, value):
            self.value = value
        def __str__(self):
            return repr(self.value)

    def load_awg(self, data, autorearm=False, continuous=False, repeats=1):
        if self.awg_site > 0:
            if self.modules[self.awg_site].task_active == '1':
                raise self.AwgBusyError("awg busy")
        port = AcqPorts.AWG_CONTINUOUS if continuous else AcqPorts.AWG_AUTOREARM if autorearm else AcqPorts.AWG_ONCE

        with netclient.Netclient(self.uut, port) as nc:
            while repeats:
                nc.sock.send(data)
                repeats -= 1
            nc.sock.shutdown(socket.SHUT_WR)
            while True:
                rx = nc.sock.recv(128)
                if not rx or rx.startswith(b"DONE"):
                    break
            nc.sock.close()

    def run_service(self, port, eof="EOF", prompt='>'):
        txt = ""
        with netclient.Netclient(self.uut, port) as nc:
            while True:
                rx = nc.receive_message(self.NL, 256)
                txt += rx
                txt += "\n"
                print("{}{}".format(prompt, rx))
                if rx.startswith(eof):
                    break
            nc.sock.shutdown(socket.SHUT_RDWR)
            nc.sock.close()

        return txt

    def run_oneshot(self):
        with netclient.Netclient(self.uut, AcqPorts.ONESHOT) as nc:
            while True:
                rx = nc.receive_message(self.NL, 256)
                print("{}> {}".format(self.s0.HN, rx))
                if rx.startswith("SHOT_COMPLETE"):
                    break
            nc.sock.shutdown(socket.SHUT_RDWR)
            nc.sock.close()

    def run_livetop(self):
        with netclient.Netclient(self.uut, AcqPorts.LIVETOP) as nc:
            print(nc.receive_message(self.NL, 256))
            nc.sock.shutdown(socket.SHUT_RDWR)
            nc.sock.close()


    def disable_trigger(self):
        #master.s0.SIG_SRC_TRG_0 = 'NONE'
        #master.s0.SIG_SRC_TRG_1 = 'NONE'
        self.s0.SIG_SRC_TRG_0 = 'HOSTB'
        self.s0.SIG_SRC_TRG_1 = 'HOSTA'

    def enable_trigger(self, trg_0='EXT', trg_1='STRIG'):
        if trg_0 is not None:
            self.s0.SIG_SRC_TRG_0 = trg_0
        if trg_1 is not None:
            self.s0.SIG_SRC_TRG_1 = trg_1

    def configure_post(self, role, trigger=[1,1,1], post=100000):
        """
        Configure UUT for a regular transient capture. Default: internal soft
        trigger starts the capture.

        "Role" is a mandatory argument. For master systems, role should be the
        string "master", if the system is a slave then role should be the string
        "slave"

        Default post samples: 100k.
        """
        print(trigger)
        self.s0.transient = "PRE=0 POST={} SOFT_TRIGGER={}".format(post, trigger[1])

        self.s1.TRG = 1
        if role == "slave" or trigger[1] == 0:
            self.s1.TRG_DX = 0
        else:
            self.s1.TRG_DX = 1
        self.s1.TRG_SENSE = trigger[2]

        self.s1.EVENT0 = 0
        self.s1.EVENT0_DX = 0
        self.s1.EVENT0_SENSE = 0

        self.s1.RGM = 0
        self.s1.RGM_DX = 0
        self.s1.RGM_SENSE = 0

        self.s1.RGM = 0 # Make sure RGM mode is turned off.
        self.s0.SIG_EVENT_SRC_0 = 0

        return None


    def configure_pre_post(self, role, trigger=[1,1,1], event=[1,1,1], pre=50000, post=100000):
        """
        Configure UUT for pre/post mode. Default: soft trigger starts the
        data flow and trigger the event on a hard external trigger.

        "Role" is a mandatory argument. For master systems, role should be the
        string "master", if the system is a slave then role should be the string
        "slave"

        Default pre trigger samples: 50k.
        Default post trigger samples: 100k.
        """
        if pre > post:
            print("PRE samples cannot be greater than POST samples. Config not set.")
            return None
        trg = 1 if trigger[1] == 1 else 0
        self.s0.transient = "PRE={} POST={} SOFT_TRIGGER={}".format(pre, post, trg)

        self.s1.TRG = trigger[0]
        if role == "slave" or trigger[1] == 0:
            self.s1.TRG_DX = 0
        else:
            self.s1.TRG_DX = 1
        self.s1.TRG_SENSE = trigger[2]

        self.s1.EVENT0 = event[0]
        self.s1.EVENT0_DX = event[1]
        self.s1.EVENT0_SENSE = event[2]

        self.s1.RGM = 0
        self.s1.RGM_DX = 0
        self.s1.RGM_SENSE = 0

        self.s1.RGM = 0 # Make sure RGM mode is turned off.
        self.s0.SIG_EVENT_SRC_0 = 0
        return None


    def configure_rtm(self, role, trigger=[1,1,1], event=[1,1,1], post=50000, rtm_translen=5000, gpg=0):
        """
        Configure UUT for rtm mode. Default: external trigger starts the capture
        and takes 5000 samples, each subsequent trigger gives us another 5000
        samples.

        "Role" is a mandatory argument. For master systems, role should be the
        string "master", if the system is a slave then role should be the string
        "slave"

        Default rtm_translen: 5k samples.
        Default post: 50k samples

        GPG can be used in RTM mode as the Event. If you are using the GPG
        then this function can put the GPG output onto the event bus (to use as
        an Event for RTM).
        """
        self.s0.transient = "PRE=0 POST={}".format(post)
        self.s1.rtm_translen = rtm_translen
        self.s1.TRG = 1
        if role == "slave" or trigger[1] == 0:
            self.s1.TRG_DX = 0
        else:
            self.s1.TRG_DX = 1
        self.s1.TRG_SENSE = trigger[2]

        self.s1.EVENT0 = event[0]
        self.s1.EVENT0_DX = event[1]
        self.s1.EVENT0_SENSE = event[2]

        self.s1.RGM = 3
        self.s1.RGM_DX = 0
        self.s1.RGM_SENSE = 1

        self.s0.SIG_EVENT_SRC_0 = 1 if gpg == 1 else 0

        return None

    def configure_transient(self, pre=0, post=100000,
        sig_DX='d0', auto_soft_trigger=0, demux=1, edge='rising'):
        """
        Configure uut for transient capture.
        sig_DX is the signal line responsible for TRIGGER or EVENT depending on mode;
        function makes appropriate selection.
        Function is aware of sync_role and sets sig_DX accordingly
        """
        sync_role = self.s0.sync_role
        if sync_role == 'role not set' and sync_role == 'slave':
            sig_DX = 'd0'

        sigdef = "1,{},{}".format(sig_DX[1], 1 if edge == 'rising' else 0)

        if pre > 0:
            self.s1.event0 = sigdef
            self.s1.trg = '1,1,1'
        else:
            self.s1.event0 = '0,0,0'
            self.s1.trg = sigdef

        self.s0.transient = "PRE={} POST={} SOFT_TRIGGER={} DEMUX={}".\
            format(pre, post, auto_soft_trigger, demux)





    def configure_rgm(self, role, trigger=[1,0,1], event=[1,1,1], post="100000", gpg=0):
        """
        Configure UUT for RGM mode. Default: external trigger starts the capture
        and the system takes samples every clock whenever the trigger is high.

        "Role" is a mandatory argument. For master systems, role should be the
        string "master", if the system is a slave then role should be the string
        "slave"

        Default post: 100k samples.

        GPG can be used in RGM mode as the Event. If you are using the GPG then
        this function can put the GPG output onto the event bus (to use as an
        Event for RGM).

        """
        self.s0.transient = "PRE=0 POST={}".format(post)
        self.s1.TRG = 1
        if role == "slave" or trigger[1] == 0:
            self.s1.TRG_DX = 0
        else:
            self.s1.TRG_DX = 1
        self.s1.TRG_SENSE = trigger[2]

        self.s1.EVENT0 = 0#event[0]
        self.s1.EVENT0_DX = 0#event[1]
        self.s1.EVENT0_SENSE = 0

        self.s1.RGM = 2
        self.s1.RGM_DX = 0
        self.s1.RGM_SENSE = 1

        self.s0.SIG_EVENT_SRC_0 = 1 if gpg == 1 else 0

        return None


    def get_demux_state(self):
        """
        Returns the current state of demux. Beware: if demux is set after the
        shot then this function will return the new state. There is no way to
        determine what the state was during the previous shot.
        """
        transient = self.s0.transient
        demux_state = transient.split("DEMUX=",1)[1][0]
        return int(demux_state)


    def pull_plot(self, channels=(), demux=-1):
        """
        Pulls data from 53000 or 5300X depending on the status of demux.
        This function takes a tuple of integers and will return the
        corresponding data from each 5300X port (if demux is on) and will return
        the corresponding data filtered from 53000 if demux is off.

        The user can also override the detected demux state if they want to: 1
        is demux on and 0 is demux off. Default is -1 and means autodetect.

        This function returns an array of the specified channels and plots the
        data.
        """
        data = []
        if demux == -1:
            demux = self.get_demux_state()
        if demux == 1:
            data = self.read_channels(channels)
        elif demux == 0:
            mux_data = self.read_muxed_data()
            print("mux data = ", mux_data)
            nchan = self.nchan()
            if channels == ():
                channels = list(range(1,nchan+1))
            for ch in channels:
                print("Channel - ", ch)
                data.append(mux_data[ch-1::nchan])

        import matplotlib.pyplot as plt
        for channel in data:
            plt.plot(channel)
        plt.grid(True)
        plt.show()
        return data


    def read_muxed_data(self):
        """
        A function that returns data from port 53000. Only use if demux is
        turned off. If demux is turned on then this function will not return the
        expected muxed data. To check if demux is enabled use the
        get_demux_state() function.
        """
        data = self.read_channels((0), -1)
        return data[0]


    def pull_data(self):
        """
        A function to pull data based on whatever demux is set to. Should be
        entirely automated. The function will check what channels are AI
        channels and pull the data from those channels.
        """

        demux_state = self.get_demux_state()
        channels = list(range(1, self.get_ai_channels()+1))
        nchan = channels[-1]

        if demux_state == 1:
            data = self.read_channels(channels, -1)
        elif demux_state == 0:
            data = []
            mux_data = self.read_muxed_data()
            for ch in channels:
                data.append(mux_data[ch-1::nchan])

        return data


    def get_ai_channels(self):
        """
        Returns all of the AI channels. This is a more robust way to get the
        total number of AI channels, as sometimes nchan can be set to include
        the scratch pad.
        """
        ai_channels = 0
        site_types = self.get_site_types()
        for ai_site in site_types["AISITES"]:
            ai_site = "s{}".format(ai_site)
            ai_channels += int(getattr(getattr(self, ai_site), "NCHAN"))

        return ai_channels

    def get_site_types(self):
        """
        Returns a dictionary with keys AISITES, AOSITES, and DIOSITES with the
        corresponding values as lists of the channels which are AI, AO, and DIO.
        """
        AISITES = []
        AOSITES = []
        DIOSITES = []

        for site in [1,2,3,4,5,6]:
            try:
                module_name = eval('self.s{}.module_name'.format(site))
                if module_name.startswith('acq'):
                    AISITES.append(site)
                elif module_name.startswith('ao'):
                    AOSITES.append(site)
                elif module_name.startswith('dio'):
                    DIOSITES.append(site)
            except Exception:
                continue

        site_types = { "AISITES": AISITES, "AOSITES": AOSITES, "DIOSITES": DIOSITES }
        return site_types

    def get_es_indices(self, file_path="default", nchan="default", human_readable=0, return_hex_string=0):
        """
        Returns the location of event samples.

        get_es_indices will pull data from a system by default (it will also
        read in a raw datafile) and reads through the data in order to find the
        location of the event samples. The system will also return the raw
        event sample data straight from the system.

        If human_readable is set to 1 then the function will return the hex
        interpretations of the event sample data. The indices will remain
        unchanged.

        If return_hex_string is set to 1 (provided human_readable has ALSO been
        set) then the function will return one single string containing all of
        the event samples.

        Data returned by the function looks like:
        [  [Event sample indices], [Event sample data]  ]
        """
        # a function that return the location of event samples.
        # returns:
        # [ [event sample indices], [ [event sample 1], ...[event sample N] ] ]
        indices = []
        event_samples = []
        nchan = self.nchan() if nchan == "default" else nchan

        if file_path == "default":
            data = self.read_muxed_data()
            data = np.array(data)
            if data.dtype == np.int16:
                # convert shorts back to raw bytes and then to longs.
                data = np.frombuffer(data.tobytes(), dtype=np.uint32)
        else:
            data = np.fromfile(file_path, dtype=np.uint32)

        if int(self.s0.data32) == 0:
            nchan = nchan / 2 # "effective" nchan has halved if data is shorts.
        nchan = int(nchan)
        for index, sample in enumerate(data[0::nchan]):
            # if sample == np.int32(0xaa55f154): # aa55
            if sample == np.uint32(0xaa55f154): # aa55
                indices.append(index)
                event_samples.append(data[index*nchan:index*nchan + nchan])

        if human_readable == 1:
            # Change decimal to hex.
            ii = 0
            while ii < len(event_samples):
                if type(event_samples[ii]) == np.ndarray:
                    event_samples[ii] = event_samples[ii].tolist()
                for indice, channel in enumerate(event_samples[ii]):
                    event_samples[ii][indice] = '0x{0:08X}'.format(channel)
                ll = int(len(event_samples[ii])/int(len(self.get_aggregator_sites())))
                # print(event_samples[ii])
                event_samples[ii] = [event_samples[ii][i:i + ll] for i in range(0, len(event_samples[ii]), ll)]
                ii += 1

            if return_hex_string == 1:
                # Make a single string containing the hex values.
                es_string = ""
                for num, sample in enumerate(event_samples):
                    for i in range(len(sample[0])):
                        for x in event_samples[num]:
                            es_string = es_string + str(x[i]) + " "
                        es_string = es_string + "\n"
                    es_string = es_string + "\n"
                event_samples = es_string

        return [indices, event_samples]
       
    def stream(self, recvlen=4096*32*2, port=AcqPorts.STREAM):
        nc = netclient.Netclient(self.uut, AcqPorts.STREAM)
        while True:
            chunk = nc.sock.recv(recvlen)
            if chunk:
                yield chunk
            else:
                break


class Acq2106(Acq400):
    """ Acq2106 specialization of Acq400

    Defines features specific to ACQ2106
    """

    def __init__(self, _uut, monitor=True, has_dsp=False, has_comms=True, has_wr=False):
        print("acq400_hapi.Acq2106 %s" % (_uut))
        Acq400.__init__(self, _uut, monitor)
        self.mb_clk_min = 100000
        sn_map = ()
        if has_comms:
            sn_map += (('cA', AcqSites.SITE_CA), ('cB', AcqSites.SITE_CB))
        if has_wr:
            sn_map += (('cC', AcqSites.SITE_CC), )
        if has_dsp:
            sn_map += (('s14', AcqSites.SITE_DSP),)

        for ( service_name, site ) in sn_map:
            try:
                self.svc[service_name] = netclient.Siteclient(self.uut, AcqPorts.SITE0+site)
            except socket.error:
                print("uut {} site {} not populated".format(_uut, site))
            self.mod_count += 1

    def set_mb_clk(self, hz=4000000, src="zclk", fin=1000000):
        print("set_mb_clk {} {} {}".format(hz, src, fin))
        Acq400.set_mb_clk(self, hz, src, fin)
        try:
            self.s0.SYS_CLK_DIST_CLK_SRC = 'Si5326'
        except AttributeError:
            print("SYS_CLK_DIST_CLK_SRC, deprecated")
        self.s0.SYS_CLK_OE_CLK1_ZYNQ = '1'

    def set_sync_routing_slave(self):
        Acq400.set_sync_routing_slave(self)
        self.s0.SYS_CLK_OE_CLK1_ZYNQ = '1'

    def set_master_trg(self, trg, edge = "rising", enabled=True):
        if trg == "fp":
            self.s0.SIG_SRC_TRG_0 = "EXT" if enabled else "HOSTB"
        elif trg == "int":
            self.s0.SIG_SRC_TRG_1 = "STRIG"


    def set_MR(self, enable, evsel0=4, evsel1=5, MR10DEC=8):
        if enable:
            self.s1.ACQ480_MR_EVSEL_0 = 'd{}'.format(evsel0)
            self.s1.ACQ480_MR_EVSEL_1 = 'd{}'.format(evsel1)
            self.s1.ACQ480_MR_10DEC = 'dec{}'.format(MR10DEC)
            self.s1.ACQ480_MR_EN = '1'
        else:
            self.s1.ACQ480_MR_EN = '0'



class Acq2106_Mgtdram8(Acq2106):
    MGT_BLOCK_BYTES = 0x400000
    MGT_BLOCK_MULTIPLE = 16

    def __init__(self, uut, monitor=True):
        print("acq400_hapi.Acq2106_MgtDram8 %s" % (uut))
        Acq2106.__init__(self, uut, monitor, has_dsp=True)

    def run_mgt(self, _filter = null_filter):
        pm = ProcessMonitor(self.uut, _filter)
        while pm.quit_requested != True:
            time.sleep(1)

    def create_mgtdram_pull_client(self):
        return MgtDramPullClient(self.uut)


class Acq2106_TIGA(Acq2106):
   
    def __init__(self, uut, monitor=True):
        print("acq400_hapi.Acq2106_TIGA %s" % (uut))
        Acq2106.__init__(self, uut, monitor, has_wr=True)
        self.pg_sites = [ sx for sx in range(1,6+1) if sx in self.sites and self.svc["s{}".format(sx)].MTYPE == '7B' ]
            
    def load_dio482pg(self, site, stl, trace = False):
        self.load_stl(stl, AcqPorts.DIO482_PG_STL+site*10, trace)
        
    def set_DO(self, site, dox, value = 'P'):
        self.svc["s{}".format(site)].set_knob("DO_{}".format(dox), value)
    

def run_unit_test():
    SERVER_ADDRESS = '10.12.132.22'
    if len(sys.argv) > 1:
        SERVER_ADDRESS = sys.argv[1]

    print("create Acq400 %s" %(SERVER_ADDRESS))
    uut = Acq400(SERVER_ADDRESS)
    print("MODEL %s" %(uut.s0.MODEL))
    print("SITELIST %s" %(uut.s0.SITELIST))
    print("MODEL %s" %(uut.s1.MODEL))

    print("Module count %d" % (uut.mod_count))
    print("POST SAMPLES %d" % uut.post_samples())

    for sx in sorted(uut.svc):
        print("SITE:%s MODEL:%s" % (sx, uut.svc[sx].sr("MODEL")))


def sigsel(enable=1, dx=1, site=None, edge=1):
    if not site is None:
        return "{},{},{}".format(enable, site+1, edge)
    else:
        return "{},{},{}".format(enable, dx, edge)

if __name__ == '__main__':
    run_unit_test()
