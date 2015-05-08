# Copyright (c) 2013-2015 Centre for Advanced Internet Architectures,
# Swinburne University of Technology. All rights reserved.
#
# Author: Sebastian Zander (szander@swin.edu.au)
#         Grenville Armitage (garmitage@swin.edu.au)
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR AND CONTRIBUTORS ``AS IS'' AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE AUTHOR OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
# OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY
# OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF
# SUCH DAMAGE.
#
## @package clockoffset
# Calculate the time offsets between different pcap files
# generated by specific TEACUP experiments
#
# Basic logic involves extracting ICMP frames that are transmitted
# roughly every 2 seconds by a local switch, and (we assume) received
# by all attached devices at the 'same time'. Hence the differences
# in the timestamps at which each ICMP frame is received gives the
# difference between the clocks of each host. At least, that's
# the idea assuming the ICMP frames are delivered at close to
# identical times on each TEACUP switch port.
#
# $Id: clockoffset.py 1288 2015-04-29 05:32:50Z szander $

import os
import socket
import csv
import tempfile
import imp
from subprocess import *
from fabric.api import task, warn, put, puts, get, local, run, execute, \
    settings, abort, hosts, env, runs_once, parallel
import config
from internalutil import _list, mkdir_p
from filefinder import get_testid_file_list

## Create safe place to dump output from stderr of various shell processes
stderrhack = os.tmpfile()

## File extension for clock offset file
CLOCK_OFFSET_FILE_EXT = '_clock_offsets.txt'

## Extension for modified data file
DATA_CORRECTED_FILE_EXT = '.tscorr'

## Temporary unzipped config
TMP_CONF_FILE = '___oldconfig.py'


## Get file with time offsets for each experiment host (TASK)
#  @param exp_list File that lists experiments to process
#  @param test_id Experiment ID
#  @param pkt_filter tcpdump filter string to filter braoadcast ping packets
#  @param baseline_host Host we compute offset against (default is first router)
#  @param out_dir Output directory for results
@task
def get_clock_offsets(exp_list='experiments_completed.txt',
                      test_id='', pkt_filter='',
                      baseline_host='',
                      out_dir=''):
    "Get clock offsets for all hosts"

    if len(out_dir) > 0 and out_dir[-1] != '/':
        out_dir += '/'

    if test_id == '':
        try:
            with open(exp_list) as f:
                test_id_arr = f.readlines()
        except IOError:
            abort('Cannot open file %s' % exp_list)
    else:
        test_id_arr = test_id.split(';')

    if len(test_id_arr) == 0 or test_id_arr[0] == '':
        abort('Must specify test_id parameter')

    # specify complete tcpdump parameter list
    tcpdump_filter = '-tt -r - -n ' + pkt_filter

    for test_id in test_id_arr:
        test_id = test_id.rstrip()

        # first find tcpdump files
        tcpdump_files = get_testid_file_list('', test_id,
                                             '_ctl.dmp.gz', '')

        if len(tcpdump_files) == 0:
            warn('No tcpdump files for control interface for %s' % test_id)
            continue

        # if we have tcpdumps for control interface we can assume broadcast ping
        # was enabled

        dir_name = os.path.dirname(tcpdump_files[0])
        # then look for tpconf_vars.log.gz file in that directory 
        var_file = local('find -L %s -name "*tpconf_vars.log.gz"' % dir_name,
                         capture=True)

	bc_addr = ''
        router = ''

        if len(var_file) > 0:
            # new approach without using config.py
            # XXX no caching here yet, assume we only generate clockoffset file once
            # per experiment 

            # unzip archived file
            local('gzip -cd %s > %s' % (var_file, TMP_CONF_FILE))

            # load the TPCONF_variables into oldconfig
            oldconfig = imp.load_source('oldconfig', TMP_CONF_FILE)

            # remove temporary unzipped file 
            try:
                os.remove(TMP_CONF_FILE)
                os.remove(TMP_CONF_FILE + 'c') # remove the compiled file as well
            except OSError:
                pass

            try:
                bc_addr = oldconfig.TPCONF_bc_ping_address
            except AttributeError:
                pass

            router_name = oldconfig.TPCONF_router[0].split(':')[0]
            
        else:
            # old approach using config.py

            try:
                bc_addr = config.TPCONF_bc_ping_address
            except AttributeError:
                pass

            router_name = config.TPCONF_router[0].split(':')[0]

        if bc_addr == '':
            # assume default multicast address 
            bc_addr = '224.0.1.199' 

        # specify complete tcpdump parameter list
        if pkt_filter != '':
            tcpdump_filter = '-tt -r - -n ' + pkt_filter
        else:
            tcpdump_filter = '-tt -r - -n ' + 'icmp and dst host ' + bc_addr

        if baseline_host == '':
            baseline_host = router_name 

        #
        # now read timestamps from each host's tcpdump
        #

        # map of host names (or IPs) and sequence numbers to timestamps
        host_times = {}
        for tcpdump_file in tcpdump_files:
            host = local(
                'echo %s | sed "s/.*_\([a-z0-9\.]*\)_ctl.dmp.gz/\\1/"' %
                tcpdump_file,
                capture=True)
            host_times[host] = {}
            #print(host)
            #print(host_times)

            # We pipe gzcat through to tcpdump. Note, since tcpdump exits early
            # (due to "-c num_samples") gzcat's pipe will collapse and gzcat
            # will complain bitterly. So we dump its stderr to stderrhack.
            init_zcat = Popen(['zcat ' + tcpdump_file], stdin=None,
                              stdout=PIPE, stderr=stderrhack, shell=True)
            init_tcpdump = Popen(['tcpdump ' + tcpdump_filter],
                                 stdin=init_zcat.stdout,
                                 stdout=PIPE,
                                 stderr=stderrhack,
                                 shell=True)

            for line in init_tcpdump.stdout.read().splitlines():
                _time = line.split(" ")[0]
                _seq = int(line.split(" ")[11].replace(',', ''))
                host_times[host][_seq] = _time

        #print(host_times)

        # get time differences and get host list
        diffs = {}
        ref_times = {}
        host_str = ''
        host_list = sorted(host_times.keys())
        # getting hosts from the config is problematic if different 
        # experiments with different configs in same directory 
        #host_list = sorted(config.TPCONF_router + config.TPCONF_hosts)

        for host in host_list:
            host_str += ' ' + host
            if host not in host_times:
                continue
            for seq in sorted(host_times[host].keys()):
                if seq not in diffs:
                    diffs[seq] = {}
                if baseline_host in host_times and seq in host_times[
                        baseline_host]:
                    diffs[seq][host] = float(host_times[host][seq]) - \
                        float(host_times[baseline_host][seq])
                    ref_times[seq] = host_times[baseline_host][seq]
                else:
                    # this should only happen if TPCONF_router was
                    # modified
                    warn('Cant find baseline host %s timestamp data' % baseline_host)
                    diffs[seq][host] = None
                    ref_times[seq] = None

        #print(diffs)

        if out_dir == '' or out_dir[0] != '/':
              dir_name = os.path.dirname(tcpdump_files[0])
              out_dir = dir_name + '/' + out_dir
        mkdir_p(out_dir)
        out_name = out_dir + test_id + CLOCK_OFFSET_FILE_EXT

        # write table of offsets (rows = time, cols = hosts)
        f = open(out_name, 'w')
        f.write('# ref_time' + host_str + '\n')
        for seq in sorted(diffs.keys()):
            if ref_times[seq] is not None:
                f.write(ref_times[seq])
            else:
                # this case should not never happen
                continue

            f.write(' ')

            for host in host_list:
                if host in diffs[seq] and diffs[seq][host] is not None:
                    f.write('{0:.6f}'.format(diffs[seq][host]))
                else:
                    f.write('NA')
                if host != host_list[-1]:
                    f.write(' ')
            f.write('\n')

        f.close()


## Adjust timestamps in interim data file (TASK)
#  @param test_id Experiment ID
#  @param file_name Interim data file
#  @param host_name Host the timestamps are from in the interim data file
#  @param sep Separator used in interim data file
#  @param out_dir Output directory for results
#  @return Name of file with corrected timestamps
@task
def adjust_timestamps(test_id='', file_name='', host_name='', sep=' ', out_dir=''):
    "Adjust timestamps in data file based on observed clock offsets"

    # out_dir is the user-specified out_dir we pass on to get_clock_offsets()
    if len(out_dir) > 0 and out_dir[-1] != '/':
        out_dir += '/'

    # out_dirname is the directory where the clockoffset file will be
    if out_dir == '' or out_dir[0] != '/':
        out_dirname = os.path.dirname(file_name)
    else:
        out_dirname = out_dir

    if out_dirname[-1] != '/':
        out_dirname += '/'

    # clock offset file name
    offs_fname = out_dirname + test_id + CLOCK_OFFSET_FILE_EXT
    # new file name
    new_fname = file_name + DATA_CORRECTED_FILE_EXT

    #print(offs_fname)

    if not os.path.isfile(offs_fname):
        execute(get_clock_offsets, test_id=test_id, out_dir=out_dir)

    if not os.path.isfile(offs_fname):
        # give up and just make a copy of the existing data, so we have a file
        # with .tscorr extension 
        #warn('Cannot generate clock offset file, using uncorrected timestamps '
        #     'for experiment %s' % test_id)
        #local('cp %s %s' % (file_name, new_fname))

        # abort so we are on the safe side, user needs to fix or rerun with
        # ts_corerct=0
        abort('Cannot generate clock offset file for experiment %s' % test_id)

        return new_fname

    host_times = []
    last_offs = 0.0
    try:
        with open(offs_fname) as f:
            offs_lines = f.readlines()

            # find column (note # is first column in first row
            host_col = -1
            for col in offs_lines[0].rstrip().split(' '):
                if col == host_name:
                    break

                host_col += 1

            #print(host_name)
            #print(host_col)

            for line in offs_lines[1:]:
                line = line.rstrip()
                ref_time = line.split(' ')[0]
                offs = line.split(' ')[host_col]
                # if we have no data our offset for correction will be the
                # last offset != zero otherwise it will be the offset measured
                if offs == 'NA':
                    offs = last_offs
                else:
                    offs = float(offs)
                    last_offs = offs

                # XXX instead of using the instantenous offset values we may
                # want to do something better in the future, such as using
                # a weighted moving average etc.
                host_times.append((ref_time, offs))

    except IOError:
        abort('Cannot open file %s' % offs_fname)

    reader = csv.reader(open(file_name, 'r'), delimiter=sep)
    fout = open(new_fname, 'w')

    # index to curr_ref_time
    curr = 0
    for line in reader:
        time = line[0]

        # find the right time, we assume here that each offset is
        # valid from the time it was observed until the time the next
        # offset is observed
        while host_times[curr][0] < time or host_times[curr][0] == 'NA':
            curr += 1
        if curr > 0 and host_times[curr - 1][0] != 'NA':
            curr -= 1

        new_time = float(time) - host_times[curr][1]

        fout.write('{0:.6f}'.format(new_time) + sep)
        fout.write(sep.join(line[1:]))
        fout.write('\n')

    fout.close()

    return new_fname
