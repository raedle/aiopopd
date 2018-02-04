import ssl
import socket
import asyncio
import logging
from asyncio import sslproto


VERSION = '0.1'
IDENT = 'Python POP3 {}'.format(VERSION)
log = logging.getLogger('aiopopd.log')
MISSING = object()


def command(state):
    def decorator(fn):
        fn.command_state = state
        return fn

    return decorator


class Pop3(asyncio.StreamReaderProtocol):
    __ident__ = 'aiopopd'

    def __init__(self, handler,
                 *,
                 hostname=None,
                 tls_context=None,
                 require_starttls=False,
                 loop=None):
        self.hostname = hostname or socket.getfqdn()
        self.loop = loop or asyncio.get_event_loop()
        super().__init__(
            asyncio.StreamReader(loop=self.loop),
            client_connected_cb=self._client_connected_cb,
            loop=self.loop)
        self.event_handler = handler
        self._original_transport = None
        self.transport = None
        self._tls_handshake_okay = True
        self._tls_protocol = None
        self.tls_context = tls_context
        if tls_context:
            # Through rfc3207 part 4.1 certificate checking is part of SMTP
            # protocol, not SSL layer.
            self.tls_context.check_hostname = False
            self.tls_context.verify_mode = ssl.CERT_NONE
        self.require_starttls = tls_context and require_starttls

    async def _call_handler_hook(self, command, *args):
        hook = getattr(self.event_handler, 'handle_' + command, None)
        if hook is None:
            return MISSING
        status = await hook(self, *args)
        return status

    def connection_made(self, transport):
        self.peer = transport.get_extra_info('peername')
        self.username = self.password = None

        seen_starttls = (self._original_transport is not None)
        if self.transport is not None and seen_starttls:
            # It is STARTTLS connection over normal connection.
            self._reader._transport = transport
            self._writer._transport = transport
            self.transport = transport
            # Do SSL certificate checking as rfc3207 part 4.1 says.  Why is
            # _extra a protected attribute?
            self.ssl = self._tls_protocol._extra
            handler = getattr(self.event_handler, 'handle_STARTTLS', None)
            if handler is None:
                self._tls_handshake_okay = True
            else:
                self._tls_handshake_okay = handler(self)
        else:
            super().connection_made(transport)
            self.transport = transport
            log.info('%r Connection opened', self.peer)
            # Process the client's requests.
            self._handler_coroutine = self.loop.create_task(
                self._handle_client())

    def connection_lost(self, error):
        log.info('%r Connection lost', self.peer)
        # If STARTTLS was issued, then our transport is the SSL protocol
        # transport, and we need to close the original transport explicitly,
        # otherwise an unexpected eof_received() will be called *after* the
        # connection_lost().  At that point the stream reader will already be
        # destroyed and we'll get a traceback in super().eof_received() below.
        if self._original_transport is not None:
            self._original_transport.close()
        super().connection_lost(error)
        self._handler_coroutine.cancel()
        self.transport = None

    def eof_received(self):
        log.info('%r EOF received', self.peer)
        self._handler_coroutine.cancel()
        if self.session.ssl is not None:            # pragma: nomswin
            # If STARTTLS was issued, return False, because True has no effect
            # on an SSL transport and raises a warning. Our superclass has no
            # way of knowing we switched to SSL so it might return True.
            #
            # This entire method seems not to be called during any of the
            # starttls tests on Windows.  I don't really know why, but it
            # causes these lines to fail coverage, hence the `nomswin` pragma
            # above.
            return False
        return super().eof_received()

    def _client_connected_cb(self, reader, writer):
        self._reader = reader
        self._writer = writer

    async def push(self, status):
        log.debug('%r %r', self.peer, status)
        response = (status + '\r\n').encode('ascii')
        self._writer.write(response)
        await self._writer.drain()

    async def push_multi(self, status, data):
        await self.push(status)
        if isinstance(data, list):
            data = b'\r\n'.join(data)
        lines = data.split(b'\r\n')
        log.debug('%r (%s lines)', self.peer, len(lines))
        for line in lines:
            if line.startswith(b'.'):
                line = b'.' + line
            response = line + b'\r\n'
            self._writer.write(response)
            await self._writer.drain()
        await self.push('.')

    async def handle_exception(self, error):
        if hasattr(self.event_handler, 'handle_exception'):
            status = await self.event_handler.handle_exception(error)
            return status
        else:
            log.exception('POP3 session exception')
            status = '-ERR Error: ({}) {}'.format(
                error.__class__.__name__, str(error))
            return status

    async def _handle_client(self):
        try:
            self.state = 'AUTHORIZATION'
            await self.push('+OK {} {}'.format(self.hostname, self.__ident__))
            while self.transport is not None:
                line = await self._reader.readline()
                log.info('%r %s', self.peer, line.split()[0])
                line = line.rstrip(b'\r\n')
                if not line:
                    await self.push('-ERR Error: bad syntax')
                    continue
                line = line.decode('ascii')
                try:
                    command, arg = line.split(' ', 1)
                except ValueError:
                    command, arg = line, None
                if not self._tls_handshake_okay and command != 'QUIT':
                    await self.push(
                        '554 Command refused due to lack of security')
                    continue
                method = getattr(self, 'pop3_' + command, None)
                method_state = getattr(method, 'command_state', None)
                if method_state is not None and method_state != self.state:
                    await self.push(
                        '-ERR wrong state for "%s"' % command)
                    continue
                if method is None:
                    await self.push(
                        '-ERR command "%s" not recognized' % command)
                    continue
                await method(arg)
        except asyncio.CancelledError:
            if self.transport is None:
                log.info('%r _handle_client() returning on CancelledError', self.peer)
            else:
                log.exception('%r Unexpected CancelledError', self.peer)
                raise
        except Exception as error:
            try:
                status = await self.handle_exception(error)
            except Exception as error:
                try:
                    log.exception('Exception in handle_exception()')
                    status = '-ERR Error: ({}) {}'.format(
                        error.__class__.__name__, str(error))
                except Exception:
                    status = '-ERR Error: Cannot describe error'
            await self.push(status)

    @staticmethod
    def parse_message_number(arg):
        if arg is None:
            raise ValueError(arg)
        n = int('+' + arg)
        if n < 1:
            raise ValueError(arg)
        return n

    async def pop3_CAPA(self, arg):
        if arg is not None:
            await self.push('-ERR Syntax: CAPA')
            return
        status = await self._call_handler_hook('CAPA')
        if status is MISSING:
            # PIPELINING?
            caps = [
                b'USER',
                b'UIDL',
            ]
            if self.tls_context:
                caps.append(b'STARTTLS')
            if hasattr(self.event_handler, 'handle_TOP'):
                caps.append(b'TOP')
            await self.push_multi('+OK Capability list follows', caps)

    @command('AUTHORIZATION')
    async def pop3_STLS(self, arg):
        if arg is not None:
            await self.push('-ERR Syntax: STLS')
            return
        if not self.tls_context:
            await self.push('-ERR TLS not available')
            return
        await self.push('+OK Begin TLS negotiation')
        # aiopopd note: the following was copied from aiosmtpd.
        # Create SSL layer.
        self._tls_protocol = sslproto.SSLProtocol(
            self.loop,
            self,
            self.tls_context,
            None,
            server_side=True)
        # Reconfigure transport layer.  Keep a reference to the original
        # transport so that we can close it explicitly when the connection is
        # lost.  XXX BaseTransport.set_protocol() was added in Python 3.5.3 :(
        self._original_transport = self.transport
        self._original_transport._protocol = self._tls_protocol
        # Reconfigure the protocol layer.  Why is the app transport a protected
        # property, if it MUST be used externally?
        self.transport = self._tls_protocol._app_transport
        self._tls_protocol.connection_made(self._original_transport)

    @command('AUTHORIZATION')
    async def pop3_USER(self, arg):
        # RFC states each arg contains no spaces and is at most 40 characters,
        # but we ignore that restriction here.
        if arg is None:
            await self.push('-ERR Syntax: USER <username>')
            return
        if self.username is not None:
            await self.push('-ERR already supplied username')
            return
        status = await self._call_handler_hook('USER', arg)
        if status is MISSING:
            self.username = arg
            status = '+OK name is a valid mailbox'
        await self.push(status)

    @command('AUTHORIZATION')
    async def pop3_PASS(self, arg):
        if arg is None:
            await self.push('-ERR Syntax: PASS <password>')
            return
        if self.username is None:
            await self.push('-ERR must supply username first')
            return
        status = await self._call_handler_hook('PASS', self.username, arg)
        if status is MISSING:
            self.password = arg
            self.state = 'TRANSACTION'
            status = '+OK'
        await self.push(status)

    @command('AUTHORIZATION')
    async def pop3_APOP(self, arg):
        status = await self._call_handler_hook('APOP', arg)
        await self.push(
            '-ERR APOP not implemented'
            if status is MISSING else status)

    async def pop3_QUIT(self, arg):
        if arg is not None:
            await self.push('-ERR Syntax: QUIT')
            return
        status = await self._call_handler_hook('QUIT')
        await self.push('+OK Bye' if status is MISSING else status)
        self._handler_coroutine.cancel()
        self.transport.close()

    @command('TRANSACTION')
    async def pop3_STAT(self, arg):
        if arg is not None:
            await self.push('-ERR Syntax: STAT')
            return
        status = await self._call_handler_hook('STAT')
        await self.push('+OK 0 0' if status is MISSING else status)

    @command('TRANSACTION')
    async def pop3_LIST(self, arg):
        if arg is None:
            n = 1
            status = '+OK scan listing follows'
            lines = []
            while True:
                try:
                    size = await self._call_handler_hook('LIST', n)
                except IndexError:
                    break
                if size is MISSING:
                    await self.push('-ERR not implemented')
                    return
                if size is not None:
                    lines.append(('%s %s' % (n, size)).encode('ascii'))
                n += 1
            await self.push_multi(status, lines)
        else:
            try:
                n = self.parse_message_number(arg)
            except ValueError:
                await self.push('-ERR Syntax: LIST [n]')
                return
            try:
                size = await self._call_handler_hook('LIST', n)
            except IndexError:
                await self.push('-ERR no such message')
                return
            if size is None:
                await self.push('-ERR no such message')
            else:
                await self.push('+OK %s %s' % (n, size))

    @command('TRANSACTION')
    async def pop3_UIDL(self, arg):
        if arg is None:
            n = 1
            status = '+OK unique-id listing follows'
            lines = []
            while True:
                try:
                    uid = await self._call_handler_hook('UIDL', n)
                except IndexError:
                    break
                if uid is MISSING:
                    await self.push('-ERR not implemented')
                    return
                if uid:
                    lines.append(('%s %s' % (n, uid)).encode('ascii'))
                n += 1
            await self.push_multi(status, lines)
        else:
            try:
                n = self.parse_message_number(arg)
            except ValueError:
                await self.push('-ERR Syntax: UIDL [n]')
                return
            try:
                uid = await self._call_handler_hook('UIDL', n)
            except IndexError:
                await self.push('-ERR no such message')
                return
            if uid is MISSING:
                await self.push('-ERR not implemented')
            elif uid:
                await self.push('+OK %s %s' % (n, uid))
            else:
                await self.push('-ERR no such message')

    @command('TRANSACTION')
    async def pop3_RETR(self, arg):
        try:
            n = self.parse_message_number(arg)
        except ValueError:
            await self.push('-ERR Syntax: RETR <n>')
            return
        try:
            status = await self._call_handler_hook('RETR', n)
        except IndexError:
            status = '-ERR no such message'
        if status is not None:
            await self.push('-ERR no such message'
                            if status is MISSING else status)

    @command('TRANSACTION')
    async def pop3_DELE(self, arg):
        try:
            n = self.parse_message_number(arg)
        except ValueError:
            await self.push('-ERR Syntax: DELE <n>')
            return
        try:
            status = await self._call_handler_hook('DELE', n)
        except IndexError:
            status = '-ERR no such message'
        await self.push('+OK deleted' if status is MISSING else status)

    @command('TRANSACTION')
    async def pop3_NOOP(self, arg):
        if arg is not None:
            await self.push('-ERR Syntax: NOOP')
            return
        status = await self._call_handler_hook('NOOP')
        await self.push('+OK' if status is MISSING else status)

    @command('TRANSACTION')
    async def pop3_RSET(self, arg):
        if arg is not None:
            await self.push('-ERR Syntax: RSET')
            return
        status = await self._call_handler_hook('RSET')
        await self.push('+OK' if status is MISSING else status)

    @command('TRANSACTION')
    async def pop3_TOP(self, arg):
        # Send headers + blank + first n lines of body
        try:
            n_str, lines_str = arg.split(' ')
            n = self.parse_message_number(n_str)
            lines = int('+' + lines_str)  # allowed to be zero
        except ValueError:
            await self.push('-ERR Syntax: TOP <n> <lines>')
            return
        status = await self._call_handler_hook('TOP', n, lines)
        if status is MISSING:
            await self.push('-ERR TOP not implemented')
