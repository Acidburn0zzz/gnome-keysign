#!/usr/bin/env python
#    Copyright 2014 Andrei Macavei <andrei.macavei89@gmail.com>
#    Copyright 2014, 2015 Tobias Mueller <muelli@cryptobitch.de>
#
#    This file is part of GNOME Keysign.
#
#    GNOME Keysign is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    GNOME Keysign is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with GNOME Keysign.  If not, see <http://www.gnu.org/licenses/>.

import logging
import os
from urlparse import urlparse, parse_qs, ParseResult
from string import Template
import shutil
from subprocess import call
from tempfile import NamedTemporaryFile

import requests
from requests.exceptions import ConnectionError

import sys

from monkeysign.gpg import Keyring
from monkeysign.gpg import GpgRuntimeError

from compat import gtkbutton
import Keyserver
from SignPages import ScanFingerprintPage, SignKeyPage, PostSignPage

import key

from gi.repository import Gst, Gtk, GLib
# Because of https://bugzilla.gnome.org/show_bug.cgi?id=698005
from gi.repository import GdkX11
# Needed for window.get_xid(), xvimagesink.set_window_handle(), respectively:
from gi.repository import GstVideo

from compat import monkeysign_expired_keys, monkeysign_revoked_keys

from .util import mac_verify


Gst.init([])

FPR_PREFIX = "OPENPGP4FPR:"
progress_bar_text = ["Step 1: Scan QR Code or type fingerprint and click on 'Download' button",
                     "Step 2: Compare the received fpr with the owner's fpr and click 'Sign'",
                     "Step 3: Key was succesfully signed and an email was sent to the owner."]


SUBJECT = 'Your signed key $fingerprint'
BODY = '''Hi $uid,


I have just signed your key

      $fingerprint


Thanks for letting me sign your key!

--
GNOME Keysign
'''


# FIXME: This probably wants to go somewhere more central.
# Maybe even into Monkeysign.
log = logging.getLogger()


def UIDExport(uid, keydata):
    """Export only the UID of a key.
    Unfortunately, GnuPG does not provide smth like
    --export-uid-only in order to obtain a UID and its
    signatures."""
    tmp = TempKeyring()
    # Hm, apparently this needs to be set, otherwise gnupg will issue
    # a stray "gpg: checking the trustdb" which confuses the gnupg library
    tmp.context.set_option('always-trust')
    tmp.import_data(keydata)
    for fpr, key in tmp.get_keys(uid).items():
        for u in key.uidslist:
            key_uid = u.uid
            if key_uid != uid:
                log.info('Deleting UID %s from key %s', key_uid, fpr)
                tmp.del_uid(fingerprint=fpr, pattern=key_uid)
    only_uid = tmp.export_data(uid)

    return only_uid


def MinimalExport(keydata):
    '''Returns the minimised version of a key

    For now, you must provide one key only.'''
    tmpkeyring = TempKeyring()
    ret = tmpkeyring.import_data(keydata)
    log.debug("Returned %s after importing %r", ret, keydata)
    assert ret
    tmpkeyring.context.set_option('export-options', 'export-minimal')
    keys_dict = tmpkeyring.get_keys()
    # We assume the keydata to contain one key only
    keys = list(keys_dict.items())
    log.debug("Keys after importing: %s (%s)", keys, keys)
    fingerprint, key = keys[0]
    stripped_key = tmpkeyring.export_data(fingerprint)
    return stripped_key



class SplitKeyring(Keyring):
    def __init__(self, primary_keyring_fname, *args, **kwargs):
        # I don't think Keyring is inheriting from object,
        # so we can't use super()
        Keyring.__init__(self)   #  *args, **kwargs)

        self.context.set_option('primary-keyring', primary_keyring_fname)
        self.context.set_option('no-default-keyring')


class TempKeyring(SplitKeyring):
    """A temporary keyring which will be discarded after use
    
    It creates a temporary file which will be used for a SplitKeyring.
    You may not necessarily be able to use this Keyring as is, because
    gpg1.4 does not like using secret keys which is does not have the
    public keys of in its pubkeyring.
    
    So you may not necessarily be able to perform operations with
    the user's secret keys (like creating signatures).
    """
    def __init__(self, *args, **kwargs):
        # A NamedTemporaryFile deletes the backing file
        self.tempfile = NamedTemporaryFile(prefix='gpgpy')
        self.fname = self.tempfile.name

        SplitKeyring.__init__(self, primary_keyring_fname=self.fname,
                                    *args, **kwargs)


class TempSigningKeyring(TempKeyring):
    """A temporary keyring which uses the secret keys of a parent keyring
    
    Creates a temporary keyring which can use the orignal keyring's
    secret keys.

    In fact, this is not much different from a TempKeyring,
    but gpg1.4 does not see the public keys for the secret keys when run with
    --no-default-keyring and --primary-keyring.
    So we copy the public parts of the secret keys into the primary keyring.
    """
    def __init__(self, base_keyring, *args, **kwargs):
        # Not a new style class...
        if issubclass(self.__class__, object):
            super(TempSplitKeyring, self).__init__(*args, **kwargs)
        else:
            TempKeyring.__init__(self, *args, **kwargs)

        # Copy the public parts of the secret keys to the tmpkeyring
        for fpr, key in base_keyring.get_keys(None,
                                              secret=True,
                                              public=False).items():
            self.import_data (base_keyring.export_data (fpr))



def openpgpkey_from_data(keydata):
    "Creates an OpenPGP object from given data"
    keyring = TempKeyring()
    if not keyring.import_data(keydata):
        raise ValueError("Could not import %r  -  stdout: %r, stderr: %r",
                         keydata,
                         keyring.context.stdout, keyring.context.stderr)
    # As we have imported only one key, we should also
    # only have one key at our hands now.
    keys = keyring.get_keys()
    if len(keys) > 1:
        log.debug('Operation on keydata "%s" failed', keydata)
        raise ValueError("Cannot give the fingerprint for more than "
            "one key: %s", keys)
    else:
        # The first (key, value) pair in the keys dict
        # next(iter(keys.items()))[0] might be semantically
        # more correct than list(d.items()) as we don't care
        # much about having a list created, but I think it's
        # more legible.
        fpr_key = list(keys.items())[0]
        # is composed of the fpr as key and an OpenPGP key as value
        key = fpr_key[1]
        return key


# FIXME: We should rename that to "from_data"
#        otherwise someone might think we operate on
#        a key rather than bytes.
def fingerprint_for_key(keydata):
    '''Returns the OpenPGP Fingerprint for a given key'''
    openpgpkey = openpgpkey_from_data(keydata)
    return openpgpkey.fpr


def get_usable_keys(keyring, *args, **kwargs):
    '''Uses get_keys on the keyring and filters for
    non revoked, expired, disabled, or invalid keys'''
    log.debug('Retrieving keys for %s, %s', args, kwargs)
    keys_dict = keyring.get_keys(*args, **kwargs)
    assert keys_dict is not None, keyring.context.stderr
    def is_usable(key):
        unusable =    key.invalid or key.disabled \
                   or key.expired or key.revoked
        log.debug('Key %s is invalid: %s (i:%s, d:%s, e:%s, r:%s)', key, unusable,
            key.invalid, key.disabled, key.expired, key.revoked)
        return not unusable
    keys_fpr = keys_dict.items()
    keys = keys_dict.values()
    usable_keys = [key for key in keys if is_usable(key)]

    log.debug('Identified usable keys: %s', usable_keys)
    return usable_keys


def get_usable_secret_keys(keyring, pattern=None):
    '''Returns all secret keys which can be used to sign a key
    
    Uses get_keys on the keyring and filters for
    non revoked, expired, disabled, or invalid keys'''
    secret_keys_dict = keyring.get_keys(pattern=pattern,
                                        public=False,
                                        secret=True)
    secret_key_fprs = secret_keys_dict.keys()
    log.debug('Detected secret keys: %s', secret_key_fprs)
    usable_keys_fprs = filter(lambda fpr: get_usable_keys(keyring, pattern=fpr, public=True), secret_key_fprs)
    usable_keys = [secret_keys_dict[fpr] for fpr in usable_keys_fprs]

    log.info('Returning usable private keys: %s', usable_keys)
    return usable_keys




## Monkeypatching to get more debug output
import monkeysign.gpg
bc = monkeysign.gpg.Context.build_command
def build_command(*args, **kwargs):
    ret = bc(*args, **kwargs)
    #log.info("Building command %s", ret)
    log.debug("Building cmd: %s", ' '.join(["'%s'" % c for c in ret]))
    return ret
monkeysign.gpg.Context.build_command = build_command




class GetKeySection(Gtk.VBox):

    def __init__(self, app):
        '''Initialises the section which lets the user
        start signing a key.

        ``app'' should be the "app" itself. The place
        which holds global app data, especially the discovered
        clients on the network.
        '''
        super(GetKeySection, self).__init__()

        self.app = app
        self.log = logging.getLogger()

        self.scanPage = ScanFingerprintPage()
        self.signPage = SignKeyPage()
        # set up notebook container
        self.notebook = Gtk.Notebook()
        self.notebook.append_page(self.scanPage, None)
        self.notebook.append_page(self.signPage, None)
        self.notebook.append_page(PostSignPage(), None)
        self.notebook.set_show_tabs(False)

        # set up the progress bar
        self.progressBar = Gtk.ProgressBar()
        self.progressBar.set_text(progress_bar_text[0])
        self.progressBar.set_show_text(True)
        self.progressBar.set_fraction(1.0/3)

        self.nextButton = Gtk.Button('Next')
        self.nextButton.connect('clicked', self.on_button_clicked)
        self.nextButton.set_image(Gtk.Image.new_from_icon_name("go-next", Gtk.IconSize.BUTTON))
        self.nextButton.set_always_show_image(True)

        self.backButton = Gtk.Button('Back')
        self.backButton.connect('clicked', self.on_button_clicked)
        self.backButton.set_image(Gtk.Image.new_from_icon_name('go-previous', Gtk.IconSize.BUTTON))
        self.backButton.set_always_show_image(True)

        bottomBox = Gtk.HBox()
        bottomBox.pack_start(self.progressBar, True, True, 0)
        bottomBox.pack_start(self.backButton, False, False, 0)
        bottomBox.pack_start(self.nextButton, False, False, 0)

        self.pack_start(self.notebook, True, True, 0)
        self.pack_start(bottomBox, False, False, 0)

        # We *could* overwrite the on_barcode function, but
        # let's rather go with a GObject signal
        #self.scanFrame.on_barcode = self.on_barcode
        self.scanPage.scanFrame.connect('barcode', self.on_barcode)
        #GLib.idle_add(        self.scanFrame.run)

        # A list holding references to temporary files which should probably
        # be cleaned up on exit...
        self.tmpfiles = []

    def set_progress_bar(self):
        page_index = self.notebook.get_current_page()
        self.progressBar.set_text(progress_bar_text[page_index])
        self.progressBar.set_fraction((page_index+1)/3.0)


    def strip_fingerprint(self, input_string):
        '''Strips a fingerprint of any whitespaces and returns
        a clean version. It also drops the "OPENPGP4FPR:" prefix
        from the scanned QR-encoded fingerprints'''
        # The split removes the whitespaces in the string
        cleaned = ''.join(input_string.split())

        if cleaned.upper().startswith(FPR_PREFIX.upper()):
            cleaned = cleaned[len(FPR_PREFIX):]

        self.log.warning('Cleaned fingerprint to %s', cleaned)
        return cleaned


    def parse_barcode(self, barcode_string):
        """Parses information contained in a barcode

        It returns a dict with the parsed attributes.
        We expect the dict to contain at least a 'fingerprint'
        entry. Others might be added in the future.
        """
        # The string, currently, is of the form
        # openpgp4fpr:foobar?baz=qux
        # Which urlparse handles perfectly fine.
        p = urlparse(barcode_string)
        fpr = p.path
        q = p.query
        rest = parse_qs(q)
        # We should probably ensure that we have only one
        # item for each parameter and flatten them accordingly.
        rest['fingerprint'] = fpr

        return rest


    def on_barcode(self, sender, barcode, message, image):
        '''This is connected to the "barcode" signal.
        The message argument is a GStreamer message that created
        the barcode.'''

        parsed = self.parse_barcode(barcode)
        fpr = parsed['fingerprint']

        if fpr != None:
            try:
                pgpkey = key.Key(fpr)
            except key.KeyError:
                self.log.exception("Could not create key from %s", barcode)
            else:
                self.log.info("Barcode signal %s %s" %( pgpkey.fingerprint, message))
                self.on_button_clicked(self.nextButton, pgpkey, message, image, parsed_barcode=parsed)
        else:
            self.log.error("data found in barcode does not match a OpenPGP fingerprint pattern: %s", barcode)


    def download_key_http(self, address, port):
        url = ParseResult(
            scheme='http',
            # This seems to work well enough with both IPv6 and IPv4
            netloc="[[%s]]:%d" % (address, port),
            path='/',
            params='',
            query='',
            fragment='')
        return requests.get(url.geturl()).text

    def try_download_keys(self, clients):
        for client in clients:
            self.log.debug("Getting key from client %s", client)
            name, address, port, fpr = client
            try:
                keydata = self.download_key_http(address, port)
                yield keydata
            except ConnectionError as e:
                # FIXME : We probably have other errors to catch
                self.log.exception("While downloading key from %s %i",
                                    address, port)

    def verify_downloaded_key(self, downloaded_data, fingerprint, mac=None):
        log.info("Verifying key %r with mac %r", fingerprint, mac)
        if mac:
            result = mac_verify(fingerprint, downloaded_data, mac)
        else:
            try:
                imported_key_fpr = fingerprint_for_key(downloaded_data)
            except ValueError:
                self.log.exception("Failed to import downloaded data")
                result = False
            else:
                if imported_key_fpr == fingerprint:
                    result = True
                else:
                    self.log.info("Key does not have equal fp: %s != %s", imported_key_fpr, fingerprint)
                    result = False

        self.log.debug("Trying to validate %s against %s: %s", downloaded_data, fingerprint, result)
        return result

    def sort_clients(self, clients, selected_client_fpr):
        key = lambda client: client[3]==selected_client_fpr
        client = sorted(clients, key=key, reverse=True)
        self.log.info("Check if list is sorted '%s'", clients)
        return clients

    def obtain_key_async(self, fingerprint, callback=None, data=None, mac=None, error_cb=None):
        self.log.debug("Obtaining key %r with mac %r", fingerprint, mac)
        other_clients = self.app.discovered_services
        self.log.debug("The clients found on the network: %s", other_clients)

        other_clients = self.sort_clients(other_clients, fingerprint)

        for keydata in self.try_download_keys(other_clients):
            if self.verify_downloaded_key(keydata, fingerprint, mac):
                is_valid = True
            else:
                is_valid = False

            if is_valid:
                # FIXME: make it to exit the entire process of signing
                # if fingerprint was different ?
                break
        else:
            self.log.error("Could not find fingerprint %s " +\
                           "with the available clients (%s)",
                           fingerprint, other_clients)
            self.log.debug("Calling error callback, if available: %s",
                            error_cb)

            if error_cb:
                GLib.idle_add(error_cb, data)
            # FIXME : don't return here
            return

        self.log.debug('Adding %s as callback', callback)
        GLib.idle_add(callback, fingerprint, keydata, data)

        # If this function is added itself via idle_add, then idle_add will
        # keep adding this function to the loop until this func ret False
        return False



    def sign_key_async(self, fingerprint=None, callback=None, data=None, error_cb=None):
        self.log.debug("I will sign key with fpr {}".format(fingerprint))

        keyring = Keyring()
        keyring.context.set_option('export-options', 'export-minimal')

        tmpkeyring = TempSigningKeyring(keyring)
        # Eventually, we want to let the user select their keys to sign with
        # For now, we just take whatever is there.
        secret_keys = get_usable_secret_keys(tmpkeyring)
        self.log.info('Signing with these keys: %s', secret_keys)

        keydata = data or self.received_key_data
        if keydata:
            stripped_key = MinimalExport(keydata)
            fpr = fingerprint_for_key(stripped_key)
            if fingerprint is None:
                # The user hasn't provided any data to operate on
                fingerprint = fpr

            if not fingerprint == fpr:
                self.log.warning('Something strange is going on. '
                    'We wanted to sign fingerprint "%s", received '
                    'keydata to operate on, but the key has fpr "%s".',
                    fingerprint, fpr)
                
        else: # Do we need this branch at all?
            if fingerprint is None:
                raise ValueError('You need to provide either keydata or a fpr')
            self.log.debug("looking for key %s in your keyring", fingerprint)
            keyring.context.set_option('export-options', 'export-minimal')
            stripped_key = keyring.export_data(fingerprint)

        self.log.debug('Trying to import key\n%s', stripped_key)
        if tmpkeyring.import_data(stripped_key):
            # 3. for every user id (or all, if -a is specified)
            # 3.1. sign the uid, using gpg-agent
            keys = tmpkeyring.get_keys(fingerprint)
            self.log.info("Found keys %s for fp %s", keys, fingerprint)
            assert len(keys) == 1, "We received multiple keys for fp %s: %s" % (fingerprint, keys)
            key = keys[fingerprint]
            uidlist = key.uidslist
            
            for secret_key in secret_keys:
                secret_fpr = secret_key.fpr
                self.log.info('Setting up to sign with %s', secret_fpr)
                # We need to --always-trust, because GnuPG would print
                # warning about the trustdb.  I think this is because
                # we have a newly signed key whose trust GnuPG wants to
                # incorporate into the trust decision.
                tmpkeyring.context.set_option('always-trust')
                tmpkeyring.context.set_option('local-user', secret_fpr)
                # FIXME: For now, we sign all UIDs. This is bad.
                ret = tmpkeyring.sign_key(uidlist[0].uid, signall=True)
                self.log.info("Result of signing %s on key %s: %s", uidlist[0].uid, fingerprint, ret)


            for uid in uidlist:
                uid_str = uid.uid
                self.log.info("Processing uid %s %s", uid, uid_str)

                # 3.2. export and encrypt the signature
                # 3.3. mail the key to the user
                signed_key = UIDExport(uid_str, tmpkeyring.export_data(uid_str))
                self.log.info("Exported %d bytes of signed key", len(signed_key))
                # self.signui.tmpkeyring.context.set_option('armor')
                tmpkeyring.context.set_option('always-trust')
                encrypted_key = tmpkeyring.encrypt_data(data=signed_key, recipient=uid_str)

                keyid = str(key.keyid())
                ctx = {
                    'uid' : uid_str,
                    'fingerprint': fingerprint,
                    'keyid': keyid,
                }
                # We could try to dir=tmpkeyring.dir
                # We do not use the with ... as construct as the
                # tempfile might be deleted before the MUA had the chance
                # to get hold of it.
                # Hence we reference the tmpfile and hope that it will be properly
                # cleaned up when this object will be destroyed...
                tmpfile = NamedTemporaryFile(prefix='gnome-keysign-', suffix='.asc')
                self.tmpfiles.append(tmpfile)
                filename = tmpfile.name
                self.log.info('Writing keydata to %s', filename)
                tmpfile.write(encrypted_key)
                # Interesting, sometimes it would not write the whole thing out,
                # so we better flush here
                tmpfile.flush()
                # As we're done with the file, we close it.
                #tmpfile.close()

                subject = Template(SUBJECT).safe_substitute(ctx)
                body = Template(BODY).safe_substitute(ctx)
                self.email_file (to=uid_str, subject=subject,
                                 body=body, files=[filename])


            # FIXME: Can we get rid of self.tmpfiles here already? Even if the MUA is still running?


            # 3.4. optionnally (-l), create a local signature and import in
            # local keyring
            # 4. trash the temporary keyring


        else:
            self.log.error('data found in barcode does not match a OpenPGP fingerprint pattern: %s', fingerprint)
            if error_cb:
                GLib.idle_add(error_cb, data)

        return False


    def send_email(self, fingerprint, *data):
        self.log.exception("Sending email... NOT")
        return False

    def email_file(self, to, from_=None, subject=None,
                   body=None,
                   ccs=None, bccs=None,
                   files=None, utf8=True):
        cmd = ['xdg-email']
        if utf8:
            cmd += ['--utf8']
        if subject:
            cmd += ['--subject', subject]
        if body:
            cmd += ['--body', body]
        for cc in ccs or []:
            cmd += ['--cc', cc]
        for bcc in bccs or []:
            cmd += ['--bcc', bcc]
        for file_ in files or []:
            cmd += ['--attach', file_]

        cmd += [to]

        self.log.info("Running %s", cmd)
        retval = call(cmd)
        return retval


    def on_button_clicked(self, button, *args, **kwargs):

        if button == self.nextButton:
            self.notebook.next_page()
            self.set_progress_bar()

            page_index = self.notebook.get_current_page()
            if page_index == 1:
                if args:
                    # If we call on_button_clicked() from on_barcode()
                    # then we get extra arguments
                    pgpkey = args[0]
                    message = args[1]
                    image = args[2]
                    fingerprint = pgpkey.fingerprint
                else:
                    image = None
                    raw_text = self.scanPage.get_text_from_textview()
                    fingerprint = self.strip_fingerprint(raw_text)

                    if fingerprint == None:
                        self.log.error("The fingerprint typed was wrong."
                        " Please re-check : {}".format(raw_text))
                        # FIXME: make it to stop switch the page if this happens
                        return

                # save a reference to the last received fingerprint
                self.last_received_fingerprint = fingerprint
                
                # Okay, this is weird.  If I don't copy() here,
                # the GstSample will get invalid.  As if it is
                # free()d although I keep a reference here.
                self.scanned_image = image.copy() if image else None

                # We also may have received a parsed_barcode" argument
                # with more information about the key to be retrieved
                barcode_information = kwargs.get("parsed_barcode", {})
                mac = barcode_information.get('mac', [None])[0] # This is a hack while the list is not flattened
                self.log.info("Transferred MAC via barcode: %r", mac)

                # error callback function
                err = lambda x: self.signPage.mainLabel.set_markup('<span size="15000">'
                        'Error downloading key with fpr\n{}</span>'
                        .format(fingerprint))
                # use GLib.idle_add to use a separate thread for the downloading of
                # the keydata.
                # Note that idle_add does not seem to take kwargs...
                # So we work around by cosntructing an anonymous function
                GLib.idle_add(lambda: self.obtain_key_async(fingerprint, self.recieved_key,
                        fingerprint, mac=mac, error_cb=err))


            if page_index == 2:
                # self.received_key_data will be set by the callback of the
                # obtain_key function. At least it should...
                # The data flow isn't very nice. It probably needs to be redone...
                GLib.idle_add(self.sign_key_async, self.last_received_fingerprint,
                    self.send_email, self.received_key_data)


        elif button == self.backButton:
            self.notebook.prev_page()
            self.set_progress_bar()


    def recieved_key(self, fingerprint, keydata, *data):
        self.received_key_data = keydata
        image = self.scanned_image
        openpgpkey = openpgpkey_from_data(keydata)
        assert openpgpkey.fpr == fingerprint
        self.signPage.display_downloaded_key(openpgpkey, fingerprint, image)
