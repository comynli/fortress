#!/bin/env python
# coding=utf-8

import os
import re
import cmd
import sys
import tty
import uuid
import time
import curses
import socket
import datetime
import threading
import getpass
import termios
import select
import etcd
import pymysql
import paramiko
from paramiko.py3compat import u


def strip_ansi_color(s):
    return re.sub(r'\x1b\[([0-9,A-Z]{1,2}(;[0-9]{1,2})?(;[0-9]{3})?)?[m|K]?', '', s)


class Db(object):
    def __init__(self, host='localhost', port=3306, user='root', passwd='', db='fortress'):
        self.conn = pymysql.connect(host=host, port=port, user=user, password=passwd, db=db)
        self.cur = self.conn.cursor()

    def __getattr__(self, item):
        return getattr(self.cur, item)

    def commit(self):
        self.conn.commit()

    def store(self, session_id, start, end, user, target):
        sql = r"INSERT INTO `audit`(`session_id`, `user`, `target`, `start`, `end`)VALUES('%s', '%s', '%s', %s, %s)"
        sql = sql % (session_id, user, target, int(time.mktime(start.timetuple())), int(time.mktime(end.timetuple())))
        try:
            self.cur.execute(sql)
            self.commit()
        except Exception as e:
            self.conn.rollback()
            print e

    def close(self):
        self.cur.close()
        self.conn.close()


class Fortress(cmd.Cmd):
    """fortress interactive shell"""
    etc_host = os.getenv("ETC_HOST", "localhost")
    etc_port = int(os.getenv("ETC_PORT", 4001))
    conf = etcd.Client(host=etc_host, port=etc_port)
    db = Db(host=conf.read('/fortress/db/host').value, port=int(conf.read('/fortress/db/port').value),
            user=conf.read('/fortress/db/user').value, passwd=conf.read('/fortress/db/passwd').value,
            db=conf.read('/fortress/db/database').value)
    user = getpass.getuser()
    host = None
    prompt = "%s@%s >>> " % (user, datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

    @classmethod
    def resize(cls, chan, c):
        while not c.is_set():
            curses.setupterm()
            chan.resize_pty(width=curses.tigetnum('cols'), height=curses.tigetnum('lines'))
            c.wait(0.1)

    @classmethod
    def timing(cls, log_txt, log_time, c):
        pos = 0
        t = 0
        while not c.is_set():
            c.wait(1/60.0)
            t += 1
            if not log_txt.closed:
                new_pos = log_txt.tell()
            blk = new_pos - pos
            if blk > 0:
                pos = new_pos
                if not log_time.closed:
                    log_time.write('%s %s\n' % (t, blk))

    @classmethod
    def posix_shell(cls, chan):
        old_tty = termios.tcgetattr(sys.stdin)
        session_id = uuid.uuid4().hex
        log_txt = open(os.path.join(cls.conf.read("/fortress/audit/txt").value, session_id), 'w')
        log_time = open(os.path.join(cls.conf.read("/fortress/audit/time").value, session_id), 'w')
        c = threading.Event()

        timer = threading.Thread(name="timer-%s" % session_id, target=cls.timing, args=(log_txt, log_time, c))
        timer.daemon = True
        start = datetime.datetime.utcnow()
        try:
            tty.setraw(sys.stdin.fileno())
            tty.setcbreak(sys.stdin.fileno())
            chan.settimeout(0.0)
            timer.start()
            while True:
                r, w, e = select.select([chan, sys.stdin], [], [])
                if chan in r:
                    try:
                        x = u(chan.recv(1024))
                        if len(x) == 0:
                            break
                        sys.stdout.write(x)
                        sys.stdout.flush()
                        log_txt.write(x)
                        log_txt.flush()
                    except socket.timeout:
                        pass
                if sys.stdin in r:
                    x = sys.stdin.read(1)
                    if len(x) == 0:
                        break
                    chan.send(x)
        finally:
            end = datetime.datetime.utcnow()
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_tty)
            c.set()
            log_time.close()
            log_txt.close()
        return session_id, start, end

    def do_open(self, ip):
        """open connection of give ip"""
        try:
            curses.setupterm()
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(ip, username=self.user, gss_auth=True)
            chan = ssh.invoke_shell(term="linux", width=curses.tigetnum('cols'), height=curses.tigetnum('lines'))
            c = threading.Event()
            refresh = threading.Thread(name="refresh", target=self.resize, args=(chan, c))
            refresh.daemon = True
            refresh.start()
            sys.stdout.write('\x1b]2;%s@%s\x07' % (self.user, ip))
            session_id, start, end = self.posix_shell(chan)
            c.set()
            self.db.store(session_id, start, end, self.user, chan.getpeername()[0])
        except Exception as e:
            print e
        sys.stdout.write('\x1b]2;%s@passport\x07' % self.user)

    def do_exit(self, arg):
        """exit shell"""
        self.db.close()
        print
        return True

    def do_EOF(self, arg):
        """exit shell"""
        self.db.close()
        print
        return True


if __name__ == '__main__':
    fortress = Fortress()
    sys.stdout.write('\x1b]2;%s@passport\x07' % fortress.user)
    intro = """
  ______         _
 |  ____|       | |
 | |__ ___  _ __| |_ _ __ ___  ___ ___
 |  __/ _ \| '__| __| '__/ _ \/ __/ __|
 | | | (_) | |  | |_| | |  __/\__ \__ \\
 |_|  \___/|_|   \__|_|  \___||___/___/

    """
    try:
        fortress.cmdloop(intro=intro)
    except KeyboardInterrupt:
        fortress.db.close()
