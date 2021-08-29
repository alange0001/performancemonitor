#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
@author: Adriano Lange <alange0001@gmail.com>

Copyright (c) 2020-present, Adriano Lange.  All rights reserved.
This source code is licensed under both the GPLv2 (found in the
LICENSE file in the root directory) and Apache 2.0 License
(found in the LICENSE.Apache file in the root directory).
"""

import os

def get_default_port():
	env_val = os.getenv('PERFMON_PORT')
	return 18087 if env_val is None or env_val == '' else int(env_val)

def get_default_interval():
	env_val = os.getenv('PERFMON_INTERVAL')
	return 5 if env_val is None or env_val == '' else int(env_val)

def get_default_device():
	env_val = os.getenv('PERFMON_DEVICE')
	return 'sda' if env_val is None or env_val == '' else env_val

def get_default_chroot():
	env_val = os.getenv('PERFMON_CHROOT')
	return '' if env_val is None else env_val
