#!/bin/sh
/sbin/pfctl -nf /etc/pf.conf && /sbin/pfctl -f /etc/pf.conf && /sbin/pfctl -E
