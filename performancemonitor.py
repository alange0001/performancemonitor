#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
@author: Adriano Lange <alange0001@gmail.com>

Copyright (c) 2020-present, Adriano Lange.  All rights reserved.
This source code is licensed under both the GPLv2 (found in the
LICENSE file in the root directory) and Apache 2.0 License
(found in the LICENSE.Apache file in the root directory).
"""

import config
import util

import argparse
import signal
import threading
import socket
import socketserver
import time
import psutil
import sys
import os
import subprocess
import shlex
import datetime
import collections
import json
import re
import traceback

#=============================================================================
import logging
from systemd.journal import JournalHandler
log = logging.getLogger('performancemonitor')
log.setLevel(logging.INFO)

#=============================================================================
args = None

class Args:
	_args = None
	def __init__(self):
		if self.__class__._args is None:
			parser = argparse.ArgumentParser(
				description="Performance monitor server.")
			parser.add_argument('-p', '--port', type=int,
				default=config.get_default_port(),
				help='device')
			parser.add_argument('-i', '--interval', type=int,
				default=5,
				help='stats interval')
			parser.add_argument('-d', '--device', type=str,
				default='sda',
				help='disk device name')
			parser.add_argument('-o', '--log_handler', type=str,
				default='stderr', choices=[ 'journal', 'stderr' ],
				help='log handler')
			parser.add_argument('-l', '--log_level', type=str,
				default='INFO', choices=[ 'debug', 'DEBUG', 'info', 'INFO' ],
				help='log level')
			parser.add_argument('-t', '--test', type=str,
				default='',
				help='test routines')
			args = parser.parse_args()
			self.__class__._args = args

			if args.interval < 1:
				raise Exception(f'parameter error: invalid interval: {args.interval}')
			args.device = args.device.split('/')[-1]
			if args.device == '':
				raise Exception(f'parameter error: invalid disk device name: "{args.device}"')

			if args.log_handler == 'journal':
				log_h = JournalHandler()
				log_h.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
			else:
				log_h = logging.StreamHandler()
				log_h.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s: %(message)s'))
			log.addHandler(log_h)

			log.setLevel(getattr(logging, args.log_level.upper()))
			log.info('Parameters: {}'.format(str(self.__class__._args)))

	def __getattr__(self, name):
		if name[0] != '_':
			return getattr(self.__class__._args, name)

#=============================================================================
class Program:
	_stop = False
	_iostat_thread = None
	_cmd_thread = None
	_current_stats = None

	def main(self):
		ret = 0
		try:
			log.info("Program initiated")
			for i in ('SIGINT', 'SIGTERM'):
				signal.signal(getattr(signal, i),  lambda signumber, stack, signame=i: self.signalHandler(signame,  signumber, stack) )

			self._iostat_thread = IOstat()
			self._iostat_thread.start()

			self._cmd_thread = CmdServer(self)

			self.collectStats()
			while not self._stop:
				time.sleep(args.interval)
				self.collectStats()

		except Exception as e:
			if log.level == logging.DEBUG:
				exc_type, exc_value, exc_traceback = sys.exc_info()
				log.critical('exception received:\n' + ''.join(traceback.format_exception(exc_type, exc_value, exc_traceback)))
			else:
				log.critical("exception received: {}".format(str(e)))
			ret = 1

		self.stop()
		return ret

	def stop(self):
		if self._stop: return
		self._stop = True
		if self._cmd_thread is not None:
			self._cmd_thread.stop()
		if self._iostat_thread is not None:
			self._iostat_thread.stop()
			self._iostat_thread.join()

	def signalHandler(self, signame, signumber, stack):
		log.warning("signal {} received".format(signame))
		self.stop()

	def collectStats(self):
		s = Stats(self._current_stats, self._iostat_thread)
		self._current_stats = s

	def currentStats(self):
		s = self._current_stats
		return s

	def clientHandler(self, handlerObj): #called by CmdServer.ThreadedTCPRequestHandler
		cur_thread = threading.current_thread()
		log.info(f"{cur_thread.name}: Client handler initiated")
		try:
			old_stats = self.currentStats()
			while True:
				message = str(handlerObj.request.recv(1024), 'utf-8')
				log.debug(f"{cur_thread.name}: message \"{message}\" received")
				if message == 'stats':
					cur_stats = old_stats
					while not self._stop and cur_stats.counter() == old_stats.counter():
						time.sleep(0.3)
						cur_stats = self.currentStats()
					if self._stop:
						break

					write_message = bytes(str(cur_stats), 'utf-8')
					old_stats = cur_stats

					log.debug("Sending stats...")
					handlerObj.request.sendall(write_message)

				elif message == 'stop' or message == 'close':
					log.info(f"{cur_thread.name}: command {message} received")
					handlerObj.request.sendall(bytes('OK, stopping', 'utf-8'))
					break
				else:
					raise Exception(f"invalid command: {message}")
		except Exception as e:
			log.error(f"{cur_thread.name}: Exception received: {str(e)}")

		log.info(f"{cur_thread.name}: Connection closed")

#=============================================================================
class CmdServer:
	_stop          = False
	_server        = None
	_server_thread = None

	def __init__(self, program):
		self.ThreadedTCPRequestHandler._program = program

		host_port = ('localhost', args.port)
		self._server = self.ThreadedTCPServer(host_port, self.ThreadedTCPRequestHandler)
		self._server_thread = threading.Thread(target=self._server.serve_forever)
		self._server_thread.daemon = True
		log.info(f'Starting {self.__class__.__name__} {host_port}')
		self._server_thread.start()
		log.debug(f'{self.__class__.__name__} started')

	def stop(self):
		if not self._stop:
			log.info(f'Stopping {self.__class__.__name__}')
			self._stop = True
			self._server.shutdown()

	# https://docs.python.org/3/library/socketserver.html
	class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
		pass
	class ThreadedTCPRequestHandler(socketserver.BaseRequestHandler):
		_program = None
		def handle(self):
			self._program.clientHandler(self)

#=============================================================================
class Stats:
	_delta_t = None
	_counter = None
	_raw_data = None
	_data = None
	_old_raw_data = None
	_old_data = None
	_disk_device = None
	_iostat = None

	def __init__(self, old, iostat):
		if old is not None:
			self._counter = old._counter+1
			self._old_raw_data = old._raw_data
			self._old_data = old._data
			self._disk_device = args.device
		else:
			self._counter = 0

		self._iostat = iostat

		self._raw_data = collections.OrderedDict()
		self._data = collections.OrderedDict()

		self.getTime()
		self.getCPU()
		self.getDisk()
		self.getFS()
		self.getContainers()

	def getTime(self):
		t = datetime.datetime.now()
		self._raw_data['time'] = t
		self._data['system_time'] = t.strftime('%Y-%m-%d %H:%M:%S')
		self._data['system_time_s'] = int(t.strftime('%s'))

		if self._old_raw_data is not None:
			self._delta_t = (t - self._old_raw_data['time']).total_seconds()

	def getCPU(self):
		#idle_times = [ 'idle', 'iowait', 'steal' ]
		def getPercents(new):
			sum_total = sum(new.values())
			ret = collections.OrderedDict()
			for k in new.keys():
				ret[k] = 100. * (new[k]/sum_total)
			return ret

		self._data['cpu'] = collections.OrderedDict()
		self._raw_data['cpu'] = collections.OrderedDict()

		self._data['cpu']['cores'] = psutil.cpu_count(logical=False)
		self._data['cpu']['threads'] = psutil.cpu_count(logical=True)
		self._data['cpu']['count'] = self._data['cpu']['threads']

		self._raw_data['cpu']['times_total'] = self._toDict(psutil.cpu_times())
		self._raw_data['cpu']['times'] = self._toDict(psutil.cpu_times(percpu=True))

		if self._old_data is not None:
			self._data['cpu']['times_total'] = self._getDiff(self._raw_data['cpu']['times_total'], self._old_raw_data['cpu']['times_total'])
			self._data['cpu']['times'] = []
			for i in range(0, len(self._raw_data['cpu']['times'])):
				self._data['cpu']['times'].append(self._getDiff(self._raw_data['cpu']['times'][i], self._old_raw_data['cpu']['times'][i]))

			self._data['cpu']['percent_total'] = getPercents(self._data['cpu']['times_total'])
			self._data['cpu']['percent'] = []
			for i in range(0, len(self._data['cpu']['times'])):
				self._data['cpu']['percent'].append(getPercents(self._data['cpu']['times'][i]))

	def getDisk(self):
		self._raw_data['disk'] = collections.OrderedDict()
		self._data['disk'] = collections.OrderedDict()
		self._raw_data['disk']['counters'] = self._toDict(psutil.disk_io_counters(perdisk=True))
		if self._old_data is not None:
			self._data['disk']['counters'] = collections.OrderedDict()
			for k, v in self._raw_data['disk']['counters'].items():
				if k == self._disk_device or k == f'/dev/{self._disk_device}':
					oldv = self._old_raw_data['disk']['counters'].get(k)
					if oldv is not None:
						self._data['disk']['counters'][k] = self._getDiff(v, oldv)

			iostat = self._iostat.getStats()
			if iostat is not None:
				self._data['disk']['iostat'] = iostat
			else:
				log.warning('iostat has no data')

	def getFS(self):
		dev_re = f'(/dev/){{0,1}}{self._disk_device}[0-9]+'
		self._data['fs'] = collections.OrderedDict()
		self._data['fs']['mount'] = []
		aux = self._toDict(psutil.disk_partitions())
		for m in aux:
			if len( re.findall(dev_re, m['device']) ) > 0:
				self._data['fs']['mount'].append(m)

		self._data['fs']['statvfs'] = collections.OrderedDict()
		dev_paths = set([x['device'] for x in self._data['fs']['mount']])
		for d in dev_paths:
			if len( re.findall(dev_re, d) ) > 0:
				for m in self._data['fs']['mount']:
					if m['device'] == d:
						self._data['fs']['statvfs'][d] = self._toDict(os.statvfs(m['mountpoint']))
						break

	def getContainers(self):
		containers = Containers().raw_data()
		self._raw_data['containers'] = containers
		self._data['containers'] = {}
		for name, data in containers.items():
			i_data = { 'name': name,
			           'id':   data['ID'], }
			self._data['containers'][name] = i_data

			if  self._old_raw_data is not None \
			and self._old_raw_data.get('containers') is not None \
			and self._old_raw_data['containers'].get(name) is not None:
				old_data = self._old_raw_data['containers'][name]

				for diff_name in ('blkio.service_bytes', 'blkio.serviced'):
					if data.get(diff_name) is None or old_data.get(diff_name) is None:
						continue
					i_data[f'{diff_name}/s'] = {}
					for k, v in data[diff_name].items():
						i_data[f'{diff_name}/s'][k] = (float(data[diff_name][k]) - float(old_data[diff_name][k])) / self._delta_t
					log.debug(f'container {name}, {diff_name}    : {data[diff_name]}')
					log.debug(f'container {name}, {diff_name} old: {old_data[diff_name]}')
					log.debug(f'container {name}, {diff_name}/s: {i_data[f"{diff_name}/s"]}')

	def _toDict(self, data):
		basetypes = (str, int, float, complex, bool)
		if isinstance(data, basetypes):
			return data

		if isinstance(data, list):
			ret = []
			for v in data:
				ret.append(self._toDict(v))
			return ret

		if isinstance(data, dict):
			ret = collections.OrderedDict()
			for k, v in data.items():
				ret[k] = self._toDict(v)
			return ret

		d = dir(data)
		if '_asdict' in d:
			return data._asdict()

		ret = collections.OrderedDict()
		for key in d:
			value = getattr(data, key)
			if key[0] != '_' and isinstance (value, basetypes):
				ret[key] = value
		return ret

	def _getDiff(self, new, old):
		ret = collections.OrderedDict()
		for k, v in new.items():
			ret[k] = v - old[k]
		return ret

	def counter(self): return self._counter

	def __str__(self):
		return 'STATS: {}'.format(json.dumps(self._data))

#=============================================================================
class IOstat (threading.Thread):
	_device = None
	_interval = None
	_started_ = False
	_stop_ = False
	_exception = None
	_proc = None
	_stats = None
	def __init__(self):
		threading.Thread.__init__(self)
		self.name = 'iostat'
		self._device = args.device
		self._interval = args.interval

	def run(self):
		log.info('Starting subprocess iostat...')
		cmd = shlex.split(f'iostat -xky -o JSON {self._interval} {self._device}')
		try:
			with subprocess.Popen(
				cmd, stdout=subprocess.PIPE, shell=False,
				stderr=subprocess.STDOUT) as proc:
				self._proc = proc
				self._started_ = True

				for l in iter(proc.stdout.readline, b''):
					s = l.decode('utf-8')
					r = re.findall(r'(\{"disk_device"[^}]+\})', s)
					if len(r) > 0:
						j = json.loads(r[0])
						self._stats = j
						log.debug('iostat: ' + str(j))
					if self._stop_: break
		except Exception as e:
			self._exception = e

		self._proc = None

	def stop(self):
		log.info('Stopping IOstat')
		if self._stop_: return
		self._stop_ = True
		if self._proc is not None:
			self._proc.kill()
			self._proc = None

	def check(self):
		if self._exception is not None:
			raise self._exception
		if self._started_ and self._proc is None:
			raise Exception('iostat is not running')

	def getStats(self):
		if not self._stop_:
			self.check()
		return self._stats

#=============================================================================
class Partitions:
	data = None
	data_major_minor = None
	def __init__(self):
		if self.__class__.data is None:
			log.debug('Partitions: load partitions')
			self.__class__.data = {}
			self.__class__.data_major_minor = {}
			with open('/proc/partitions', 'r') as fd:
				lines = fd.readlines()
				for l in lines:
					r = re.findall(r'\s([0-9]+)\s+([0-9]+)\s+([0-9]+)\s+(.+)', l)
					if len(r) > 0:
						d = { 'name':  r[0][3],
						      'major': r[0][0],
						      'minor': r[0][1],
						      'blocks':r[0][2], }
						self.__class__.data[d['name']] = d
						self.__class__.data_major_minor[f"{d['major']}:{d['minor']}"] = d

	def __getitem__(self, idx):
		r = self.__class__.data.get(idx)
		if r is not None: return r
		r = self.__class__.data_major_minor[idx]
		if r is not None: return r
		raise Exception(f'partition "{idx}" not found')

#=============================================================================
class Containers:
	_container_names = None
	_container_ids   = None
	def __init__(self):
		cmd = "docker ps --format '{{json . }}'"
		exitcode, output = subprocess.getstatusoutput(cmd)
		if exitcode != 0:
			raise Exception(f'docker returned error {exitcode}')

		partition = Partitions()[args.device]
		major_minor = f'{partition["major"]}:{partition["minor"]}'
		log.debug(f'major_minor = {major_minor}')

		self._container_names = {}
		self._container_ids   = {}
		for l in output.splitlines():
			j = json.loads(l)
			id = j['ID']
			self._container_names[j['Names']] = j
			self._container_ids[id] = j

			aux = self.get_blkio(major_minor, f'/sys/fs/cgroup/blkio/docker/{id}*/blkio.throttle.io_service_bytes')
			if aux is not None: j['blkio.service_bytes'] = aux

			aux = self.get_blkio(major_minor, f'/sys/fs/cgroup/blkio/docker/{id}*/blkio.throttle.io_serviced')
			if aux is not None: j['blkio.serviced'] = aux

	def get_blkio(self, major_minor, filename):
		ret = {}
		cmd = f"cat {filename}"
		exitcode, output = subprocess.getstatusoutput(cmd)
		if exitcode != 0:
			log.error(f'get_blkio command "{cmd}" returned error {exitcode}')
			return None
		for l in output.splitlines():
			r = re.findall(f'{major_minor} ([^ ]+) (.*)', l)
			if len(r) > 0:
				ret[r[0][0]] = r[0][1]
		return ret

	def names(self):
		return self._container_names.keys()
	def ids(self):
		return self._container_ids.keys()
	def raw_data(self):
		return self._container_names
	def __getitem__(self, idx):
		r = self._container_names.get(idx)
		if r is not None: return r
		return self._container_ids[idx]

#=============================================================================
class Test:
	def __init__(self, name):
		f = getattr(self, name)
		if f is None:
			raise Exception(f'test named "{name}" does not exist')
		f()

	def args(self):
		a = Args()
		log.debug(a._args)

	def stats(self):
		io = IOstat()
		io.start()
		s = Stats(None, io)
		for i in range(0,4):
			time.sleep(args.interval)
			s = Stats(s, io)
			log.debug(str(s))
		io.stop()

	def iostat(self):
		io = IOstat()
		io.start()
		for i in range(0,2):
			time.sleep(args.interval)
			log.debug(io.getStats())
		io.stop()

	def partitions(self):
		p = Partitions()
		log.debug(p.data)

	def containers(self):
		d = Containers()
		log.debug(d.names())
		log.debug(d.ids())
		log.debug(d._container_names)

#=============================================================================
if __name__ == '__main__':
	r = 0
	try:
		args = Args()
		if args.test == '':
			r = Program().main()
		else:
			Test(args.test)

		log.info('exit {}'.format(r))

	except Exception as e:
		if log.level == logging.DEBUG:
			exc_type, exc_value, exc_traceback = sys.exc_info()
			sys.stderr.write('main exception:\n' + ''.join(traceback.format_exception(exc_type, exc_value, exc_traceback)) + '\n')
		else:
			sys.stderr.write(str(e) + '\n')
		exit(1)

	exit(r)
