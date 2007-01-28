import os
import threading
import Queue
import traceback
import atexit
import weakref
import __future__

# note that the whole code of this module (as well as some
# other modules) execute not only on the local side but 
# also on any gateway's remote side.  On such remote sides
# we cannot assume the py library to be there and 
# InstallableGateway.remote_bootstrap_gateway() (located 
# in register.py) will take care to send source fragments
# to the other side.  Yes, it is fragile but we have a 
# few tests that try to catch when we mess up. 

# XXX the following lines should not be here
if 'ThreadOut' not in globals(): 
    import py 
    from py.code import Source
    from py.__.execnet.channel import ChannelFactory, Channel
    from py.__.execnet.message import Message
    ThreadOut = py._thread.ThreadOut 
    WorkerPool = py._thread.WorkerPool 
    NamedThreadPool = py._thread.NamedThreadPool 

import os
debug = 0 # open('/tmp/execnet-debug-%d' % os.getpid()  , 'wa')

sysex = (KeyboardInterrupt, SystemExit)

class Gateway(object):
    num_worker_threads = 2
    ThreadOut = ThreadOut 

    def __init__(self, io, startcount=2, maxthreads=None):
        global registered_cleanup
        self._execpool = WorkerPool() 
##        self.running = True 
        self.io = io
        self._outgoing = Queue.Queue()
        self.channelfactory = ChannelFactory(self, startcount)
##        self._exitlock = threading.Lock()
        if not registered_cleanup:
            atexit.register(cleanup_atexit)
            registered_cleanup = True
        _active_sendqueues[self._outgoing] = True
        self.pool = NamedThreadPool(receiver = self.thread_receiver, 
                                    sender = self.thread_sender)

    def __repr__(self):
        addr = self._getremoteaddress()
        if addr:
            addr = '[%s]' % (addr,)
        else:
            addr = ''
        try:
            r = (len(self.pool.getstarted('receiver'))
                 and "receiving" or "not receiving")
            s = (len(self.pool.getstarted('sender')) 
                 and "sending" or "not sending")
            i = len(self.channelfactory.channels())
        except AttributeError:
            r = s = "uninitialized"
            i = "no"
        return "<%s%s %s/%s (%s active channels)>" %(
                self.__class__.__name__, addr, r, s, i)

    def _getremoteaddress(self):
        return None

##    def _local_trystopexec(self):
##        self._execpool.shutdown() 

    def _trace(self, *args):
        if debug:
            try:
                l = "\n".join(args).split(os.linesep)
                id = getid(self)
                for x in l:
                    print >>debug, x
                debug.flush()
            except sysex:
                raise
            except:
                traceback.print_exc()

    def _traceex(self, excinfo):
        try:
            l = traceback.format_exception(*excinfo)
            errortext = "".join(l)
        except:
            errortext = '%s: %s' % (excinfo[0].__name__,
                                    excinfo[1])
        self._trace(errortext)

    def thread_receiver(self):
        """ thread to read and handle Messages half-sync-half-async. """
        try:
            from sys import exc_info
            while 1:
                try:
                    msg = Message.readfrom(self.io)
                    self._trace("received <- %r" % msg)
                    msg.received(self)
                except sysex:
                    raise
                except EOFError:
                    break
                except:
                    self._traceex(exc_info())
                    break 
        finally:
            self._outgoing.put(None)
            self.channelfactory._finished_receiving()
            self._trace('leaving %r' % threading.currentThread())

    def thread_sender(self):
        """ thread to send Messages over the wire. """
        try:
            from sys import exc_info
            while 1:
                msg = self._outgoing.get()
                try:
                    if msg is None:
                        self.io.close_write()
                        break
                    msg.writeto(self.io)
                except:
                    excinfo = exc_info()
                    self._traceex(excinfo)
                    if msg is not None:
                        msg.post_sent(self, excinfo)
                    raise
                else:
                    self._trace('sent -> %r' % msg)
                    msg.post_sent(self)
        finally:
            self._trace('leaving %r' % threading.currentThread())

    def _local_redirect_thread_output(self, outid, errid): 
        l = []
        for name, id in ('stdout', outid), ('stderr', errid): 
            if id: 
                channel = self.channelfactory.new(outid)
                out = ThreadOut(sys, name)
                out.setwritefunc(channel.send) 
                l.append((out, channel))
        def close(): 
            for out, channel in l: 
                out.delwritefunc() 
                channel.close() 
        return close 

    def thread_executor(self, channel, (source, outid, errid)):
        """ worker thread to execute source objects from the execution queue. """
        from sys import exc_info
        try:
            loc = { 'channel' : channel }
            self._trace("execution starts:", repr(source)[:50])
            close = self._local_redirect_thread_output(outid, errid) 
            try:
                co = compile(source+'\n', '', 'exec',
                             __future__.CO_GENERATOR_ALLOWED)
                exec co in loc
            finally:
                close() 
                self._trace("execution finished:", repr(source)[:50])
        except (KeyboardInterrupt, SystemExit):
            raise
        except:
            excinfo = exc_info()
            l = traceback.format_exception(*excinfo)
            errortext = "".join(l)
            channel.close(errortext)
            self._trace(errortext)
        else:
            channel.close()

    def _local_schedulexec(self, channel, sourcetask): 
        self._trace("dispatching exec")
        self._execpool.dispatch(self.thread_executor, channel, sourcetask) 

    def _newredirectchannelid(self, callback): 
        if callback is None: 
            return  
        if hasattr(callback, 'write'): 
            callback = callback.write 
        assert callable(callback) 
        chan = self.newchannel()
        chan.setcallback(callback)
        return chan.id 

    # _____________________________________________________________________
    #
    # High Level Interface
    # _____________________________________________________________________
    #
    def newchannel(self): 
        """ return new channel object.  """ 
        return self.channelfactory.new()

    def remote_exec(self, source, stdout=None, stderr=None): 
        """ return channel object for communicating with the asynchronously
            executing 'source' code which will have a corresponding 'channel'
            object in its executing namespace. 
        """
        try:
            source = str(Source(source))
        except NameError: 
            try: 
                import py 
                source = str(py.code.Source(source))
            except ImportError: 
                pass 
        channel = self.newchannel() 
        outid = self._newredirectchannelid(stdout) 
        errid = self._newredirectchannelid(stderr) 
        self._outgoing.put(Message.CHANNEL_OPEN(channel.id, 
                               (source, outid, errid)))
        return channel 

    def remote_redirect(self, stdout=None, stderr=None): 
        """ return a handle representing a redirection of a remote 
            end's stdout to a local file object.  with handle.close() 
            the redirection will be reverted.   
        """ 
        clist = []
        for name, out in ('stdout', stdout), ('stderr', stderr): 
            if out: 
                outchannel = self.newchannel()
                outchannel.setcallback(getattr(out, 'write', out))
                channel = self.remote_exec(""" 
                    import sys
                    outchannel = channel.receive() 
                    outchannel.gateway.ThreadOut(sys, %r).setdefaultwriter(outchannel.send)
                """ % name) 
                channel.send(outchannel)
                clist.append(channel)
        for c in clist: 
            c.waitclose(1.0) 
        class Handle: 
            def close(_): 
                for name, out in ('stdout', stdout), ('stderr', stderr): 
                    if out: 
                        c = self.remote_exec("""
                            import sys
                            channel.gateway.ThreadOut(sys, %r).resetdefault()
                        """ % name) 
                        c.waitclose(1.0) 
        return Handle()

##    def exit(self):
##        """ initiate full gateway teardown.   
##            Note that the  teardown of sender/receiver threads happens 
##            asynchronously and timeouts on stopping worker execution 
##            threads are ignored.  You can issue join() or join(joinexec=False) 
##            if you want to wait for a full teardown (possibly excluding 
##            execution threads). 
##        """ 
##        # note that threads may still be scheduled to start
##        # during our execution! 
##        self._exitlock.acquire()
##        try:
##            if self.running: 
##                self.running = False 
##                if not self.pool.getstarted('sender'): 
##                    raise IOError("sender thread not alive anymore!") 
##                self._outgoing.put(None)
##                self._trace("exit procedure triggered, pid %d " % (os.getpid(),))
##                _gateways.remove(self) 
##        finally:
##            self._exitlock.release()

    def exit(self):
        self._outgoing.put(None)
        try:
            del _active_sendqueues[self._outgoing]
        except KeyError:
            pass

    def join(self, joinexec=True):
        current = threading.currentThread()
        for x in self.pool.getstarted(): 
            if x != current: 
                self._trace("joining %s" % x)
                x.join()
        self._trace("joining sender/reciver threads finished, current %r" % current) 
        if joinexec: 
            self._execpool.join()
            self._trace("joining execution threads finished, current %r" % current) 

def getid(gw, cache={}):
    name = gw.__class__.__name__
    try:
        return cache.setdefault(name, {})[id(gw)]
    except KeyError:
        cache[name][id(gw)] = x = "%s:%s.%d" %(os.getpid(), gw.__class__.__name__, len(cache[name]))
        return x

registered_cleanup = False
_active_sendqueues = weakref.WeakKeyDictionary()
def cleanup_atexit():
    if debug:
        print >>debug, "="*20 + "cleaning up" + "=" * 20
        debug.flush()
    while True:
        try:
            queue, ignored = _active_sendqueues.popitem()
        except KeyError:
            break
        queue.put(None)
