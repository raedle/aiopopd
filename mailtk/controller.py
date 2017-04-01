import asyncio
from mailtk.data import Mailbox, ThreadInfo
import traceback
import pprint


class ThreadAccount(ThreadInfo):
    _fields = 'account'

    @property
    def children(self):
        return [ThreadAccount(c, self.account)
                for c in self.inner_threadinfo.children]

class MailboxAccount(Mailbox):
    _fields = 'account'


class Controller:
    def __init__(self, loop, accounts, gui):
        self.loop = loop
        self.accounts = accounts
        self.gui = gui
        self.gui.controller = self
        self.init_accounts_result = self.ensure_future(self.init_accounts())
        self.pending_interaction = None

    def ensure_future(self, coro):
        async def wrapper():
            try:
                return await coro
            except Exception:
                self.handle_exception()

        return asyncio.ensure_future(wrapper(), loop=self.loop)

    async def init_accounts(self):
        self.gui.set_accounts(self.accounts.keys())
        account_coros = []
        for k, v in self.accounts.items():
            account_coros.append(self.init_account(k, v))
        await asyncio.gather(*account_coros, loop=self.loop,
                             return_exceptions=True)

    async def init_account(self, account_name, get_account):
        try:
            account = await get_account(self)
        except Exception:
            self.log_exception("Failed to connect to %r" %
                               (account_name,))
            return
        try:
            mailboxes = await account.list_folders()
            self.log_debug(repr(mailboxes))
            assert all(isinstance(f, Mailbox) for f in mailboxes)
            folders = [MailboxAccount(f, account) for f in mailboxes]
            self.gui.set_folders(account_name, folders)
            self.folders = folders
        except Exception:
            self.log_exception('Failed to initialize account %r' %
                               (account_name,))

    def handle_exception(self):
        self.log_exception('Unhandled exception caught by mailtk.Controller')

    def log_exception(self, msg):
        s = traceback.format_exc()
        if msg:
            s = '\n\n'.join((msg, traceback.format_exc()))
        self.log_debug(s)

    def log_debug(self, msg):
        self.gui.log_debug(msg)

    def set_interaction(self, coro):
        if self.pending_interaction and not self.pending_interaction.done():
            self.pending_interaction.cancel()
        self.pending_interaction = self.ensure_future(coro)

    def set_selected_folder(self, account, folder):
        self.set_interaction(self._set_selected_folder(account, folder))

    async def _set_selected_folder(self, account_name, folder):
        mailbox, account = folder
        result = await account.list_messages(mailbox)
        result = [ThreadAccount(thread, account)
                  for thread in result]
        self.gui.set_threads(result)
        self.gui.set_message(None)

    def set_selected_thread(self, thread):
        self.set_interaction(self._set_selected_thread(thread))

    async def _set_selected_thread(self, thread):
        self.log_debug(repr(thread))
        self.log_debug('Fetching %r...' % (thread.subject,))
        message = await thread.account.fetch_message(thread.inner_threadinfo)
        self.gui.set_message(message)
        # mailbox, account = folder
        # result = await account.list_messages(mailbox)
        # self.gui.set_threads(result)
