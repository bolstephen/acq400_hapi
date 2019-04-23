#!/usr/bin/env python


"""
This is a regression test suite that uses a function generator and the onboard
GPG to test UUTs. For more information on regression tests please refer to the
D-TACQ wiki page:

http://eigg-fs:8090/mediawiki/index.php/Products:ACQ400:Regression_Testing

Usage:

python regression-test-suite.py --trg=int --test=post --channels="1,1" acq2106_085 acq2106_126

python regression-test-suite.py --trg=ext --test=post --channels="1,1" acq2106_085 acq2106_126



"""

from __future__ import print_function
import acq400_hapi
import numpy as np
import os
import time
import argparse
import socket
from future import builtins
import matplotlib.pyplot as plt


def create_rtm_stl():
    stl =  "0,f\n \
    10000,0\n \
    20000,f\n \
    30000,0\n \
    40000,f\n \
    50000,0\n \
    60000,f\n \
    70000,0\n \
    80000,f\n \
    90000,0\n \
    100000,f\n \
    110000,0\n \
    120000,f\n \
    130000,0\n \
    140000,f\n \
    150000,0\n \
    160000,f\n \
    170000,0\n \
    180000,f\n \
    190000,0\n \
    200000,f\n \
    210000,0\n \
    220000,f\n \
    230000,0\n \
    240000,f\n \
    250000,0\n \
    260000,f\n \
    270000,0\n \
    280000,f\n \
    290000,0"
    return stl


def create_rgm_stl():
    # An example STL file for regression test purposes.
    stl = "0,f\n \
    5000,0\n \
    20000,f\n \
    35000,0\n \
    40000,f\n \
    45000,0\n \
    60000,f\n \
    75000,0\n \
    80000,f\n \
    200000,0"
    return stl


def calculate_frequency(args, uut, divisor):
    # calculate a reasonable frequency from the clock speed of the master uut.
    freq = str(int(float(uut.s0.SIG_CLK_S1_FREQ.split(" ")[1]) / divisor))
    return freq


def trigger_system(args, sig_gen):
    # if "rtm" not in args.test:
    if args.test != "rtm":
        print("Triggering now.")
        sig_gen.send("TRIG\n".encode())
        if args.trg == "ext" and args.test == "pre_post":
            time.sleep(2)
            sig_gen.send("TRIG\n".encode())
    return None


def config_gpg(uut, args, trg=1):
    # The following settings are very test specific and so they
    # have not been included in a library function.
    uut.s0.gpg_enable = 0
    uut.s0.gpg_clk = "1,1,1" # GPG clock is the same as the site.
    uut.s0.gpg_trg = "1,{},1".format(trg)
    uut.s0.gpg_mode = 3 # LOOPWAIT

    if args.test == "rgm":
        stl = create_rgm_stl()
    else:
        stl = create_rtm_stl()
    uut.load_gpg(stl)
    uut.s0.gpg_enable = 1
    return None


def configure_sig_gen(sig_gen, args, freq):
    print("Configuring sig gen.")

    sig_gen.send("VOLT 1\n".encode())
    sig_gen.send("OUTP:SYNC ON\n".encode())
    freq_string = "FREQ {}\n".format(freq)
    sig_gen.send(freq_string.encode())
    sig_gen.send("FUNC:SHAP SIN\n".encode())

    if args.test == "post":
        if args.trg == "ext":
            sig_gen.send("BURS:STAT ON\n".encode())
            sig_gen.send("BURS:NCYC 1\n".encode())
            sig_gen.send("TRIG:SOUR BUS\n".encode())
        else:
            sig_gen.send("BURS:STAT OFF\n".encode())
            sig_gen.send("TRIG:SOUR IMM\n".encode())

    if args.test == "pre_post":
        sig_gen.send("BURS:STAT ON\n".encode())
        sig_gen.send("BURS:NCYC 1\n".encode())
        if args.trg == "ext":
            sig_gen.send("TRIG:SOUR BUS\n".encode())
        else:
            sig_gen.send("TRIG:SOUR IMM\n".encode())

    elif args.test == "rtm" or args.test == "rgm":
        # sig_gen.send("FREQ 1000\n".encode())
        sig_gen.send("BURS:STAT ON\n".encode())
        sig_gen.send("BURS:NCYC 1\n".encode())
        sig_gen.send("TRIG:SOUR IMM\n".encode())
        if args.test == "rgm":
            sig_gen.send("BURS:NCYC 5\n".encode())
            sig_gen.send("TRIG:SOUR BUS\n".encode())
    elif args.test == "rtm_gpg":
        sig_gen.send("TRIG:SOUR IMM\n".encode())
        sig_gen.send("BURS:STAT OFF\n".encode())
        sig_gen.send("FUNC:SHAP RAMP\n".encode())
        sig_gen.send("FREQ 1\n".encode())
    return None


def check_master_slave(args, uut):
    print(uut.s0.HN, uut.s0.sync_role)
    return None


def check_es(events):
    success_flag = True
    for uut_es in events:
        if uut_es == events[0]:
            continue
        else:
            print("\nES comparison FAILED!\n")
            success_flag = False
            return False
    if success_flag == True:
        print("\nES Comparison successful!\n")


def show_es(events, uuts):
    # length = len(events[0][1])
    # for index, uut in enumerate(uuts):
    #     print('{HN: {fill}{align}{length}}'.format(HN=uut.s0.HN, fill=' ', align='<', length=length))
        # events[index][1] = '{0: <{length}}'.format(uut.s0.HN + "\n", length=length) + events[index][1]
    uut_list = list(range(0, len(uuts)))
    lines = [events[counter][1].splitlines() for counter in uut_list]
    for l in zip(*lines):
        print(*l, sep='')
    return None


def save_data(uuts):
    for uut in uuts:
        data = uut.read_muxed_data()
        data[0].tofile("{}_shot_data".format(uut.s0.HN))
    return None


def run_test(args):
    uuts = []
    data = []
    events = []

    for uut in args.uuts:
        uut = acq400_hapi.Acq400(uut)
        uut.s0.set_abort
        uut.s0.transient = "DEMUX={}".format(args.demux)
        check_master_slave(args, uut)
        uuts.append(uut)

    if args.config_sig_gen == 1:
        sig_gen = socket.socket()
        sig_gen.connect((args.sig_gen_name, 5025))
        freq = calculate_frequency(args, uuts[0], args.clock_divisor)
        configure_sig_gen(sig_gen, args, freq)

    for index, uut in reversed(list(enumerate(uuts))):

        if args.test == "pre_post":
            if index == 0:
                uut.configure_pre_post("master", trigger=args.trg)
            else:
                # uut.s0.sync_role = "slave"
                uut.configure_pre_post("slave")

        elif args.test == "post":
            if index == 0:
                uut.configure_post("master", trigger=args.trg)
            else:
                uut.configure_post("slave", trigger=args.trg)

        elif args.test == "rtm":
            if index == 0:
                uut.configure_rtm("master", trigger=args.trg)
            else:
                uut.configure_rtm("slave")

        elif args.test == "rtm_gpg":
            if index == 0:
                uut.configure_rtm("master", trigger=args.trg, gpg=1)
                config_gpg(uut, args, trg=0)
            else:
                uut.configure_rtm("slave")

        elif args.test == "rgm":
            if index == 0:
                uut.configure_rgm("master", trigger=args.trg, post=75000, gpg=1)
                config_gpg(uut, args, trg=0)
            else:
                uut.s0.sync_role = "slave"
                uut.configure_rgm("slave", post=75000)

        uut.s0.set_arm
        uut.statmon.wait_armed()

    time.sleep(5)

    trigger_system(args, sig_gen)

    channels = args.channels.split(",")

    for index, uut in enumerate(uuts):
        # uut.statmon.wait_stopped()
        acq400_hapi.shotcontrol.wait_for_state(uut, "IDLE")
        data.append(uut.read_channels(int(channels[index])))
        events.append(uut.get_es_indices(human_readable=1, return_hex_string=1))

    if args.demux == 0:
        check_es(events)
        if args.show_es == 1:
            show_es(events, uuts)
        save_data(uuts)

    else:
        for data_set in data:
            for ch in data_set:
                plt.plot(ch)
        plt.grid(True)
        plt.show()

    # import code
    # code.interact(local=locals())

    return None


def run_main():
    parser = argparse.ArgumentParser(description='acq400 regression test.')

    parser.add_argument('--test', default="pre_post", type=str,
    help='Which test to run. Options are: pre_post, rtm, rgm.')

    parser.add_argument('--trg', default="ext", type=str,
    help='Which trigger to use. Options are ext and int.')

    parser.add_argument('--config_sig_gen', default=1, type=int,
    help='If True, configure signal generator.')

    parser.add_argument('--sig_gen_name', default="A-33600-00001", type=str,
    help='Default IP address.')

    parser.add_argument('--channels', default=1, type=str,
    help='Which channel to pull from each UUT in order. Format: 1,17,...,x.')

    parser.add_argument('--clock_divisor', default=20000, type=int,
    help="The speed at which to run the sig gen. 20,000 is human readable.")

    parser.add_argument('--demux', default=1, type=int,
    help="Whether or not to have demux configured on the UUT.")

    parser.add_argument('--show_es', default=1, type=int,
    help="Whether or not to show the event samples when demux = 0.")

    parser.add_argument('uuts', nargs='+', help="Names of uuts to test.")

    run_test(parser.parse_args())


if __name__ == '__main__':
    run_main()
