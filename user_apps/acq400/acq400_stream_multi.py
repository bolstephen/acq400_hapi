#!/usr/bin/env python

"""
This is a script intended to connect to a UUT and stream data from port 4210.

The data that has been streamed is not demuxed and so if it is to be used then it has to be demuxed first.
Something like:

    >>> data = numpy.fromfile("0000", dtype="<datatype>")
    >>> plt.plot(data[::<number of channels>])
    >>> plt.show()

usage::
    acq400_stream.py [-h] [--filesize FILESIZE] [--totaldata TOTALDATA]
                        [--root ROOT] [--runtime RUNTIME] [--verbose VERBOSE]
                        uuts [uuts ...]

acq400 stream

positional arguments:
  uuts                  uuts

optional arguments:
  -h, --help            show this help message and exit
  --filesize FILESIZE   Size of file to store in KB. If filesize > total data
                        then no data will be stored.
  --totaldata TOTALDATA
                        Total amount of data to store in KB
  --root ROOT           Location to save files
  --runtime RUNTIME     How long to stream data for
  --verbose VERBOSE     Prints status messages as the stream is running


Some usage examples are included below:

1: Acquire files of size 1024kb up to a total of 4096kb:


    >>> python acq400_stream.py --verbose=1 --filesize=1M --totaldata=4M <module ip or name>

2: Acquire a single file of size 4096kb:


    >>> python acq400_stream.py --verbose=1 --filesize=4M --totaldata=4M <module ip or name>

3: Acquire files of size 1024 for 10 seconds:


    >>> python acq400_stream.py --verbose=1 --filesize=1M --runtime=10 <module ip or name>

4: Acquire data for 5 seconds and write the data all to a single file:


    >>> python acq400_stream.py --verbose=1 --filesize=9999M --runtime=5 <module ip or name>

"""

import acq400_hapi
import numpy as np
import os
import time
import argparse
import socket
import sys

import multiprocessing

def make_data_dir(directory, verbose):
    if verbose:
        print("make_data_dir {}".format(directory))
    try:
        os.makedirs(directory)
    except Exception:
        if verbose:
            print("Directory already exists")
        pass


class StreamsOne:
    def __init__ (self, args, uut_name):
        self.args = args
        self.uut_name = uut_name
    def run(self):        
        uut = acq400_hapi.Acq400(self.uut_name)
        cycle = -1
        num = 999       # force initial directory create
        data_length = 0
        if self.args.filesize > self.args.totaldata:
            self.args.filesize = self.args.totaldata
        try:
            if int(uut.s0.data32):
                data_size = 4
                wordsizetype = "<i4"  # 32 bit little endian
            else:
                wordsizetype = "<i2"  # 16 bit little endian
                data_size = 2
        except AttributeError:
            print("Attribute error detected. No data32 attribute - defaulting to 16 bit")
            wordsizetype = "<i2"  # 16 bit little endian
            data_size = 2
            
        start_time = time.time()
            
        for buf in uut.stream(recvlen=self.args.filesize, data_size=data_size):
            data_length += len(buf)
            if num > 99:
                num = 0
                cycle += 1
                root = os.path.join(self.args.root, self.uut_name, "{:06d}".format(cycle))
                make_data_dir(root, self.args.verbose)

            data_file = open(os.path.join(root, "{:04d}.dat".format(num)), "wb")
            buf.tofile(data_file, '')

            if self.args.verbose == 1:
                print("New data file written.")
                print("Data Transferred: ", data_length, "KB")
                print("Streaming time remaining: ", -1*(time.time() - (start_time + self.args.runtime)))
                print("")
                print("")

            num += 1
                
            if time.time() >= (start_time + self.args.runtime) or data_length > self.args.totaldata:                
                return
            
               
    
def run_stream(args):
    RXBUF_LEN = 4096
    cycle = 1
    root = args.root + args.uuts[0] + "/" + "{:06d}".format(cycle)
    data = bytes()
    num = 0
        
    for uut_name in reversed(args.uuts):
        streamer = StreamsOne(args, uut_name)
        if len(args.uuts) > 1 and uut_name == args.uuts[0]:
            print("Pause before launching M")
            time.sleep(2)
        multiprocessing.Process(target=streamer.run).start()
        #streamer.run()

def run_main():
    parser = argparse.ArgumentParser(description='acq400 stream')
    #parser.add_argument('--filesize', default=1048576, type=int,
    #                    help="Size of file to store in KB. If filesize > total data then no data will be stored.")
    parser.add_argument('--filesize', default=0x100000, action=acq400_hapi.intSIAction, decimal=False)
    parser.add_argument('--totaldata', default=10000000000, action=acq400_hapi.intSIAction, decimal = False)
    #parser.add_argument('--totaldata', default=4194304, type=int, help="Total amount of data to store in KB")
    parser.add_argument('--root', default="", type=str, help="Location to save files. Default dir is UUT name.")
    parser.add_argument('--runtime', default=1000000, type=int, help="How long to stream data for")
    parser.add_argument('--verbose', default=0, type=int, help='Prints status messages as the stream is running')
    parser.add_argument('uuts', nargs='+', help="uuts")

    run_stream(parser.parse_args())


if __name__ == '__main__':
    run_main()
