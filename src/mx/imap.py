import imaplib
import logging
import re
from contextlib import contextmanager, ExitStack
from select import select

logger = logging.getLogger(__name__)


class IMAP(imaplib.IMAP4_SSL):

    @contextmanager
    def mailbox(self, name, readonly=False):
        """
        Mailbox context manager helper, selects on enter and closes on exit
        """
        logger.debug('IMAP: open mailbox [%s]', name)
        status, (details,) = self.select(name, readonly=readonly)
        # status, (details,) = self.select('foobar', readonly=False)

        if status != 'OK':
            raise self.error(details)

        try:
            yield
        finally:
            if self.state == 'SELECTED':
                logger.debug('IMAP: close mailbox [%s]', name)
                self.close()

    def subscribe(self, callback, mailbox='INBOX'):
        """
        Subscribes (blocking) for new mail events using IDLE mode.
        Notifying callback when found.
        """
        with self.mailbox(mailbox, readonly=True):
            count = self._get_exists_response()

            for _ in self.idle():
                new_count = self._get_exists_response()

                if new_count:
                    logger.info("IMAP: \u2709 You've got mail (%+i)", new_count - count)
                    count = new_count
                    callback()
                else:
                    expunge = self._get_expunge_response()
                    if expunge is not None:
                        count = expunge

    def fetch_unseen(self, mailbox='INBOX', touch=True):
        """
        Selecting and searching mailbox for unseen mails.
        Yields raw messages together with their mailbox sequence number and UID.

        :param touch: Flag found messages as seen
        """
        with self.mailbox(mailbox, readonly=(not touch)):
            criteria = '(UNSEEN)'
            logger.debug('IMAP: search %s', criteria)
            _, (result,) = self.search(None, criteria)

            if result:
                indices = b','.join(result.split())  # Message sequence number formatted
                logger.debug('IMAP: fetch messages [%s]', indices.decode())
                _, data = self.fetch(indices, '(UID RFC822)')

                for _type, RFC822 in data[::2]:
                    match = re.match(r'(?P<index>\d+) \(UID (?P<uid>\d+).*', _type.decode())

                    index = match.group('index')
                    uid = match.group('uid')

                    logger.debug('IMAP: fetched message #%s [UID:%s]', index, uid)
                    yield index, uid, RFC822

    def mark_unseen(self, indices):
        """
        Flag message(s) as unseen.

        :param indices: Message sequence number(s) in format: 2,10:12,15 means 2,10,11,12,15
        """
        self.store(indices, '-FLAGS.SILENT', '\\Seen')

    def idle(self, timeout=29*60):
        """
        Enters IDLE mode and yields lines sent from server.
        Closes and re-enters IDLE mode every <timeout> second.

        :param timeout: IMAP4 RFC says restart IDLE every 29 min
        """
        while 1:
            try:
                tag = self._idle_command()

                timer = 0
                select_timeout = 1
                idling = True

                while idling:
                    ready, _, _ = select([self.file], [], [], select_timeout)
                    # ready = True

                    if ready:
                        # Socket got bytes to read
                        response = self._get_response()
                        self._check_bye()
                        if response:
                            yield response
                    else:
                        # Timeout
                        timer += select_timeout
                        if timer >= timeout:
                            logger.debug('IMAP: idle timeout')
                            idling = False

            finally:
                if self.state == 'IDLING':
                    self._done_command(tag)

    def _get_exists_response(self):
        _, exists = self._untagged_response('OK', [None], 'EXISTS')
        count = exists[-1]
        if count:
            return int(count)

    def _get_expunge_response(self):
        _, expunge = self._untagged_response('OK', [None], 'EXPUNGE')
        count = expunge[-1]
        if count:
            return int(count)

    def _idle_command(self):
        """
        Enter IDLE mode
        """
        if 'IDLE' not in imaplib.Commands:
            imaplib.Commands['IDLE'] = ('SELECTED',)

        try:
            logger.debug('IMAP: idle')
            tag = self._command('IDLE')
        finally:
            self.state = 'IDLING'
        return tag

    def _done_command(self, tag):
        """
        Exit IDLE mode
        """
        try:
            logger.debug('IMAP: stop idling')
            self.send(b'DONE' + imaplib.CRLF)

            # Validate IDLE mode exited correctly
            status, data = self._command_complete('IDLE', tag)
            if not status == 'OK':
                raise self.error(data)
        finally:
            self.state = 'SELECTED'


class login(object):
    """
    IMAP context manager.
    Connect, login and returns client.
    Cleanups states and resources on exit.
    """
    def __init__(self, host, username, password, debug_level=0):
        self.client = None
        self.host = host
        self.username = username
        self.password = password
        self.debug = debug_level

    def __enter__(self):
        with ExitStack() as stack:  # Ensures __exit__ is called
            stack.push(self)

            # Create IMAP client and connect
            if not self.client:
                logger.debug('IMAP: connect [%s]', self.host)
                self.client = IMAP(host=self.host)
                self.client.debug = self.debug

            # Login
            if self.client.state == 'NONAUTH':
                logger.debug('IMAP: login [%s]', self.username)
                self.client.login(self.username, self.password)

            stack.pop_all()

            return self.client

    def __exit__(self, exception, message, stacktrace):
        # Cleanup
        if self.client:
            # Logout
            if self.client.state == 'AUTH':
                logger.debug('IMAP: logout / disconnect')
                self.client.logout()  # Calls shutdown as well

            # Shutdown (if not done through logout)
            if self.client.state == 'NONAUTH':
                logger.debug('IMAP: disconnect')
                self.client.shutdown()

            self.client = None

        if exception:
            if exception is InterruptedError:
                pass  # Do not handle as standard OSError -> Bubble

            elif issubclass(exception, (OSError, IMAP.abort)):
                raise ConnectionError(message)  # Merge network errors and IMAP aborts

            elif issubclass(exception, IMAP.error):
                raise ValueError(message)  # TODO: Better alternative
