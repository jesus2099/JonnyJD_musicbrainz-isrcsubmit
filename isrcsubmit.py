#!/usr/bin/env python2
# Copyright (C) 2010-2013 Johannes Dewender
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""This is a tool to submit ISRCs from a disc to MusicBrainz.

Various backends are used to gather the ISRCs
and python-musicbrainz2 to submit them.
The project is hosted on
https://github.com/JonnyJD/musicbrainz-isrcsubmit
"""

isrcsubmit_version = "1.0.0"
agent_name = "isrcsubmit.py"
# starting with highest priority
backends = ["mediatools", "media_info", "discisrc", "cdrdao", "cd-info",
            "cdda2wav", "icedax", "drutil"]
packages = {"cd-info": "libcdio", "cdda2wav": "cdrtools", "icedax": "cdrkit"}

import os
import re
import sys
import codecs
import getpass
import tempfile
from datetime import datetime
from optparse import OptionParser
from subprocess import Popen, PIPE, call

import discid
import musicbrainzngs
from musicbrainzngs import AuthenticationError, WebServiceError

# using a shellscript to get the correct python version (2.5 - 2.7)
shellname = "isrcsubmit.sh"
if os.path.isfile(shellname):
    scriptname = shellname
else:
    scriptname = os.path.basename(sys.argv[0])

# make code more Python 3 compliant for easier backporting
# this still won't run on Python 3
try:
    user_input = raw_input
except NameError:
    user_input = input

def script_version():
    return "isrcsubmit %s by JonnyJD for MusicBrainz" % isrcsubmit_version

def print_help(option=None, opt=None, value=None, parser=None):
    print(\
"""
This python script extracts ISRCs from audio cds and submits them to MusicBrainz (musicbrainz.org).
You need to have a MusicBrainz account, specify the username and will be asked for your password every time you execute the script.

Isrcsubmit will warn you if there are any problems and won't actually submit anything to MusicBrainz without giving a final choice.

Isrcsubmit will warn you if any duplicate ISRCs are detected and help you fix priviously inserted duplicate ISRCs.
The ISRC-track relationship we found on our disc is taken as our correct evaluation.
""")
    parser.print_usage()
    print("""\
Please report bugs on https://github.com/JonnyJD/musicbrainz-isrcsubmit""")
    sys.exit(0)


class Isrc(object):
    def __init__(self, isrc, track=None):
        self._id = isrc
        self._tracks = []
        if track is not None:
            self._tracks.append(track)

    def add_track(self, track):
        if track not in self._tracks:
            self._tracks.append(track)

    def get_tracks(self):
        return self._tracks

    def get_track_numbers(self):
        numbers = []
        for track in self._tracks:
            numbers.append(track.getNumber())
        return ", ".join([str(number) for number in numbers])


class EqTrack(object):
    """track with equality checking

    This makes it easy to check if this track is already in a collection.
    Only the element already in the collection needs to be hashable.

    """
    def __init__(self, track):
        self._track = track
        self._recording = track["recording"]

    def __eq__(self, other):
        return self.getId() == other.getId()

    def getId(self):
        return self._recording["id"]

    def getArtist(self):
        return self._recording.get("artist-credit-phrase")

    def getTitle(self):
        return self._recording["title"]

    def getISRCs(self):
        return self._recording.get("isrc-list", [])

class NumberedTrack(EqTrack):
    """A track found on an analyzed (own) disc

    """
    def __init__(self, track, number):
        EqTrack.__init__(self, track)
        self._number = number

    def getNumber(self):
        """The track number on the analyzed disc"""
        return self._number

class OwnTrack(NumberedTrack):
    """A track found on an analyzed (own) disc

    """
    pass

def gather_options(argv):
    global options

    if os.name == "nt":
        default_device = "D:"
        # this is "cdaudio" in libdiscid, but no user understands that..
        # cdrdao is not given a device and will try 0,1,0
        # this default is only for libdiscid and mediatools
    else:
        default_device = discid.DEFAULT_DEVICE
    default_browser = "firefox"
    prog = scriptname
    parser = OptionParser(version=script_version(), add_help_option=False)
    parser.set_usage("%s [options] [user] [device]\n       %s -h" % (prog,
                                                                     prog))
    parser.add_option("-h", action="help",
            help="Short usage help")
    parser.add_option("--help", action="callback", callback=print_help,
            help="Complete help for the script")
    parser.add_option("-u", "--user", metavar="USERNAME",
            help="MusicBrainz username, if not given as argument.")
    # note that -d previously stand for debug
    parser.add_option("-d", "--device", metavar="DEVICE",
            help="CD device with a loaded audio cd, if not given as argument."
            + " The default is " + default_device + " (and '1' for mac)")
    parser.add_option("-b", "--backend", choices=backends, metavar="PROGRAM",
            help="Force using a specific backend to extract ISRCs from the"
            + " disc. Possible backends are: %s." % ", ".join(backends)
            + " They are tried in this order otherwise." )
    parser.add_option("--browser", metavar="BROWSER",
            help="Program to open urls. The default is " + default_browser)
    parser.add_option("--debug", action="store_true", default=False,
            help="Show debug messages."
            + " Currently shows some backend messages.")
    (options, args) = parser.parse_args(argv[1:])

    # assign positional arguments to options
    if options.user is None and args:
        options.user = args[0]
        args = args[1:]
    if options.device is None:
        if args:
            options.device = args[0]
            args = args[1:]
        else:
            # Mac: device is changed again, when we know the final backend
            # Win: cdrdao is not given a device and will try 0,1,0
            options.device = default_device
    if options.browser is None:
        options.browser = default_browser
    if args:
        print("WARNING: Superfluous arguments: %s" % ", ".join(args))
    options.sane_which = test_which()
    if options.backend and not has_backend(options.backend, strict=True):
        print_error("Chosen backend not found. No ISRC extraction possible!")
        print_error2("Make sure that %s is installed." % options.backend)
        sys.exit(-1)

    return options


def test_which():
    """There are some old/buggy "which" versions on Windows.
    We want to know if the user has a "sane" which we can trust.
    Unxutils has a broken 2.4 version. Which >= 2.16 should be fine.
    """
    devnull = open(os.devnull, "w")
    try:
        # "which" should at least find itself (even without searching which.exe)
        return_code = call(["which", "which"], stdout=devnull, stderr=devnull)
    except OSError:
        return False        # no which at all
    else:
        if (return_code == 0):
            return True
        else:
            print('warning: your version of the tool "which" is buggy/outdated')
            if os.name == "nt":
                print('         unxutils is old/broken, GnuWin32 is good.')
            return False

def get_prog_version(prog):
    if prog == "icedax":
        return Popen([prog, "--version"], stderr=PIPE).communicate()[1].strip()
    elif prog == "cdda2wav":
        outdata = Popen([prog, "-version"], stdout=PIPE).communicate()[0]
        return " ".join(outdata.splitlines()[0].split()[0:2])
    elif prog == "cdrdao":
        outdata = Popen([prog], stderr=PIPE).communicate()[1]
        return " ".join(outdata.splitlines()[0].split()[::2][0:2])
    elif prog == "cd-info":
        outdata = Popen([prog, "--version"], stdout=PIPE).communicate()[0]
        return " ".join(outdata.splitlines()[0].split()[::2][0:2])
    elif prog == "drutil":
        outdata = Popen([prog, "version"], stdout=PIPE).communicate()[0]
        version = prog
        for line in outdata.splitlines():
            if line:
                version += " " + line.split(":")[1].strip()
        return version
    else:
        return prog

def has_backend(backend, strict=False):
    """When the backend is only a symlink to another backend,
       we will return False, unless we strictly want to use this backend.
    """
    devnull = open(os.devnull, "w")
    if options.sane_which:
        p_which = Popen(["which", backend], stdout=PIPE, stderr=devnull)
        backend_path = p_which.communicate()[0].strip()
        if p_which.returncode == 0:
            # check if it is only a symlink to another backend
            real_backend = os.path.basename(os.path.realpath(backend_path))
            if backend != real_backend and real_backend in backends: 
                if strict:
                    print("WARNING: %s is a symlink to %s" % (backend,
                                                              real_backend))
                    return True
                else:
                    return False # use real backend instead, or higher priority
            return True
        else:
            return False
    else:
        try:
            # we just try to start these non-interactive console apps
            call([backend], stdout=devnull, stderr=devnull)
        except OSError:
            return False
        else:
            return True

def get_real_mac_device(option_device):
    """drutil takes numbers as drives.

    We ask drutil what device name corresponds to that drive
    in order so we can use it as a drive for libdiscid
    """
    p = Popen(["drutil", "status", "-drive", option_device], stdout=PIPE)
    try:
        given = p.communicate()[0].splitlines()[3].split("Name:")[1].strip()
    except IndexError:
        print_error("could not find real device")
        print_error2("maybe there is no disc in the drive?")
        sys.exit(-1)
    # libdiscid needs the "raw" version
    return given.replace("/disk", "/rdisk")

def askForOffset(disc_track_count, release_track_count):
    limit = release_track_count - disc_track_count
    while True:
        # ask until a correct offset is given (or a KeyboardInterrupt)
        print("")
        print("How many tracks are on the previous (actual) discs altogether?")
        try:
            choice = user_input("[0-%d] " % limit)
        except KeyboardInterrupt:
            print("\nexiting..")
            sys.exit(1)
        try:
            num = int(choice)
        except ValueError:
            print_error("Not a number")
        else:
            if num in range(0, limit + 1):
                return num

def cp65001(name):
    """This might be buggy, but better than just a LookupError
    """
    if name.lower() == "cp65001":
        return codecs.lookup("utf-8")

codecs.register(cp65001)

def printf(format_string, *args):
    """Print with the % and without additional spaces or newlines
    """
    if not args:
        # make it convenient to use without args -> different to C
        args = (format_string, )
        format_string = "%s"
    sys.stdout.write(format_string % args)

def print_encoded(*args):
    """This will replace unsuitable characters and doesn't append a newline
    """
    stringArgs = ()
    for arg in args:
        if isinstance(arg, unicode):
            stringArgs += arg.encode(sys.stdout.encoding, "replace"),
        else:
            stringArgs += str(arg),
    msg = " ".join(stringArgs)
    if not msg.endswith("\n"):
        msg += " "
    if os.name == "nt":
        os.write(sys.stdout.fileno(), msg)
    else:
        sys.stdout.write(msg)

def print_error(*args):
    string_args = tuple([str(arg) for arg in args])
    msg = " ".join(("ERROR:",) + string_args)
    sys.stderr.write(msg + "\n")

def print_error2(*args):
    """following lines for print_error()"""
    string_args = tuple([str(arg) for arg in args])
    msg = " ".join(("      ",) + string_args)
    sys.stderr.write(msg + "\n")

def backend_error(backend, err):
    print_error("Couldn't gather ISRCs with %s: %i - %s"
                % (backend, err.errno, err.strerror))
    sys.exit(1)

class WebService2():
    """A web service wrapper that asks for a password when first needed.

    This uses musicbrainzngs as a wrapper itself.
    """

    def __init__(self, username=None):
        self.auth = False
        self.username = username
        musicbrainzngs.set_useragent(agent_name, isrcsubmit_version,
                "http://github.com/JonnyJD/musicbrainz-isrcsubmit")

    def authenticate(self):
        """Sets the password if not set already
        """
        if not self.auth:
            print("")
            if self.username is None:
                printf("Please input your MusicBrainz username: ")
                self.username = user_input()
            printf("Please input your MusicBrainz password: ")
            password = getpass.getpass("")
            print("")
            musicbrainzngs.auth(self.username, password)
            self.auth = True

    def get_releases_by_discid(self, disc_id, includes=[]):
        response = musicbrainzngs.get_releases_by_discid(disc_id,
                            includes=includes)
        return response["disc"]["release-list"]

    def get_release_by_id(self, release_id, includes=[]):
        return musicbrainzngs.get_release_by_id(release_id, includes=includes)

    def submit_isrcs(self, tracks2isrcs):
        self.authenticate()
        musicbrainzngs.submit_isrcs(tracks2isrcs)



class Disc(object):
    def read_disc(self):
        try:
            # calculate disc ID from disc
            with discid.DiscId() as disc:
                disc.read(self._device)
                self._id = disc.id
                self._submission_url = disc.submission_url
                self._track_count = len(disc.track_offsets) - 1
        except DiscError as err:
            print_error("DiscID calculation failed: %s" % err)
            sys.exit(1)

    def __init__(self, device, verified=False):
        self._device = device
        self._release = None
        self._verified = verified
        self.read_disc()        # sets self._id etc.

    @property
    def id(self):
        return self._id

    @property
    def track_count(self):
        return self._track_count

    @property
    def submission_url(self):
        return self._submission_url

    @property
    def release(self):
        """The corresponding MusicBrainz release

        This will ask the user to choose if the discID is ambiguous.
        """
        if self._release is None:
            self._release = self.getRelease(self._verified)
            # can still be None
        return self._release

    def getRelease(self, verified=False):
        """Find the corresponding MusicBrainz release

        This will ask the user to choose if the discID is ambiguous.
        """
        try:
            includes=["artists", "labels", "recordings", "isrcs",
                      "artist-credits"] # the last one only for cleanup
            results = ws2.get_releases_by_discid(self.id, includes=includes)
        except WebServiceError as err:
            print_error("Couldn't fetch release: %s" % err)
            sys.exit(1)
        num_results = len(results)
        if num_results == 0:
            print("This Disc ID is not in the database.")
            self._release = None
        elif num_results > 1:
            print("This Disc ID is ambiguous:")
            for i in range(num_results):
                # TODO: list mediums, not releases
                # possible a discID is in multiple mediums of a release
                # (that would indicate a problem in the DB)
                release = results[i]
                # printed list is 1..n, not 0..n-1 !
                print_encoded("%d: %s - %s (%s)\n"
                              % (i + 1, release["artist-credit-phrase"],
                                 release["title"], release["status"]))
                country = (release.get("country") or "").ljust(2)
                date = (release.get("date") or "").ljust(10)
                barcode = (release.get("barcode") or "").rjust(13)
                label_list = release["label-info-list"]
                catnumber_list = []
                for label in label_list:
                    cat_number = label.get("catalog-number")
                    if cat_number:
                        catnumber_list.append(cat_number)
                catnumbers = ", ".join(catnumber_list)
                print_encoded("\t%s\t%s\t%s\t%s\n"
                              % (country, date, barcode, catnumbers))
            try:
                num =  user_input("Which one do you want? [1-%d] "
                                  % num_results)
                if int(num) not in range(1, num_results + 1):
                    raise IndexError
                self._release = results[int(num) - 1]
            except (ValueError, IndexError):
                print_error("Invalid Choice")
                sys.exit(1)
            except KeyboardInterrupt:
                print("\nexiting..")
                sys.exit(1)
        else:
            self._release = results[0]

        if self._release and self._release["id"] is None:
            # a "release" that is only a stub has no musicbrainz id
            print("\nThere is only a stub in the database:")
            print_encoded("%s - %s\n\n"
                          % (self._release["artist-credit-phrase"],
                             self._release["title"]))
            self._release = None        # don't use stub
            verified = True             # the id is verified by the stub

        if self._release is None:
            if verified:
                url = self.submission_url
                printf("Would you like to open the browser to submit the disc?")
                if user_input(" [y/N] ") == "y":
                    try:
                        if os.name == "nt":
                            # silly but necessary for spaces in the path
                            os.execlp(options.browser,
                                    '"' + options.browser + '"', url)
                        else:
                            # linux/unix works fine with spaces
                            os.execlp(options.browser, options.browser, url)
                    except OSError as err:
                        print_error("Couldn't open the url in %s: %s"
                                    % (options.browser, str(err)))
                        print_error2("Please submit it via:", url)
                        sys.exit(1)
                else:
                    print("Please submit the Disc ID with this url:")
                    print(url)
                    sys.exit(1)
            else:
                print("recalculating to re-check..")
                self.read_disc()
                self.getRelease(verified=True)

        return self._release

def get_disc(device, verified=False):
    """This creates a Disc object, which also calculates the id of the disc
    """
    disc = Disc(device, verified)
    print('\nDiscID:\t\t%s' % disc.id)
    print('Tracks on disc:\t%d' % disc.track_count)
    return disc


def gather_isrcs(backend, device):
    """read the disc in the device with the backend and extract the ISRCs
    """
    backend_output = []
    devnull = open(os.devnull, "w")

    if backend == "discisrc":
        pattern = \
            r'Track\s+([0-9]+)\s+:\s+([A-Z]{2})-?([A-Z0-9]{3})-?(\d{2})-?(\d{5})'
        try:
            if sys.platform == "darwin":
                device = get_real_mac_device(device)
            p = Popen([backend, device], stdout=PIPE)
            isrcout = p.stdout
        except OSError as err:
            backend_error(backend, err)
        for line in isrcout:
            if debug:
                printf(line)    # already includes a newline
            if line.startswith("Track") and len(line) > 12:
                m = re.search(pattern, line)
                if m == None:
                    print("can't find ISRC in: %s" % line)
                    continue
                track_number = int(m.group(1))
                isrc = m.group(2) + m.group(3) + m.group(4) + m.group(5)
                backend_output.append((track_number, isrc))

    # icedax is a fork of the cdda2wav tool
    elif backend in ["cdda2wav", "icedax"]:
        pattern = \
            r'T:\s+([0-9]+)\sISRC:\s+([A-Z]{2})-?([A-Z0-9]{3})-?(\d{2})-?(\d{5})'
        try:
            p1 = Popen([backend, '-J', '-H', '-D', device], stderr=PIPE)
            p2 = Popen(['grep', 'ISRC'], stdin=p1.stderr, stdout=PIPE)
            isrcout = p2.stdout
        except OSError as err:
            backend_error(backend, err)
        for line in isrcout:
            # there are \n and \r in different places
            if debug:
                printf(line)    # already includes a newline
            for text in line.splitlines():
                if text.startswith("T:"):
                    m = re.search(pattern, text)
                    if m == None:
                        print("can't find ISRC in: %s" % text)
                        continue
                    track_number = int(m.group(1))
                    isrc = m.group(2) + m.group(3) + m.group(4) + m.group(5)
                    backend_output.append((track_number, isrc))

    elif backend == "cd-info":
        pattern = \
            r'TRACK\s+([0-9]+)\sISRC:\s+([A-Z]{2})-?([A-Z0-9]{3})-?(\d{2})-?(\d{5})'
        try:
            p = Popen([backend, '-T', '-A', '--no-device-info', '--no-cddb',
                '-C', device], stdout=PIPE)
            isrcout = p.stdout
        except OSError as err:
            backend_error(backend, err)
        for line in isrcout:
            if debug:
                printf(line)    # already includes a newline
            if line.startswith("TRACK"):
                m = re.search(pattern, line)
                if m == None:
                    print("can't find ISRC in: %s" % line)
                    continue
                track_number = int(m.group(1))
                isrc = m.group(2) + m.group(3) + m.group(4) + m.group(5)
                backend_output.append((track_number, isrc))

    # media_info is a preview version of mediatools, both are for Windows
    elif backend in ["mediatools", "media_info"]:
        pattern = \
            r'ISRC\s+([0-9]+)\s+([A-Z]{2})-?([A-Z0-9]{3})-?(\d{2})-?(\d{5})'
        if backend == "mediatools":
            args = [backend, "drive", device, "isrc"]
        else:
            args = [backend, device]
        try:
            p = Popen(args, stdout=PIPE)
            isrcout = p.stdout
        except OSError as err:
            backend_error(backend, err)
        for line in isrcout:
            if debug:
                printf(line)    # already includes a newline
            if line.startswith("ISRC") and not line.startswith("ISRCS"):
                m = re.search(pattern, line)
                if m == None:
                    print("can't find ISRC in: %s" % line)
                    continue
                track_number = int(m.group(1))
                isrc = m.group(2) + m.group(3) + m.group(4) + m.group(5)
                backend_output.append((track_number, isrc))

    # cdrdao will create a temp file and we delete it afterwards
    # cdrdao is also available for windows
    elif backend == "cdrdao":
        pattern = r'[A-Z]{2}[A-Z0-9]{3}\d{2}\d{5}'
        tmpname = "cdrdao-%s.toc" % datetime.now()
        tmpname = tmpname.replace(":", "-")     # : is invalid on windows
        tmpfile = os.path.join(tempfile.gettempdir(), tmpname)
        if debug:
            print("Saving toc in %s.." % tmpfile)
        if os.name == "nt" and device != "D:":
            print("warning: cdrdao uses the default device")
            args = [backend, "read-toc", "--fast-toc", "-v", "0", tmpfile]
        else:
            args = [backend, "read-toc", "--fast-toc", "--device", device,
                "-v", "0", tmpfile]
        try:
            p = Popen(args, stdout=devnull, stderr=devnull)
            if p.wait() != 0:
                print_error("%s returned with %i" % (backend, p.returncode))
                sys.exit(1)
        except OSError as err:
            backend_error(backend, err)
        else:
            with open(tmpfile, "r") as toc:
                track_number = None
                for line in toc:
                    if debug:
                        printf(line)    # already includes a newline
                    words = line.split()
                    if words:
                        if words[0] == "//":
                            track_number = int(words[2])
                        elif words[0] == "ISRC" and track_number is not None:
                            isrc = "".join(words[1:]).strip('"- ')
                            m = re.match(pattern, isrc)
                            if m is None:
                                print("no valid ISRC: %s" % isrc)
                            elif isrc:
                                backend_output.append((track_number, isrc))
                                # safeguard against missing trackNumber lines
                                # or duplicated ISRC tags (like in CD-Text)
                                track_number = None
        finally:
            try:
                os.unlink(tmpfile)
            except OSError:
                pass

    # this is the backend included in Mac OS X
    # it will take a lot of time because it scans the whole disc
    elif backend == "drutil":
        pattern = \
        r'Track\s+([0-9]+)\sISRC:\s+([A-Z]{2})-?([A-Z0-9]{3})-?(\d{2})-?(\d{5})'
        try:
            p1 = Popen([backend, 'subchannel', '-drive', device], stdout=PIPE)
            p2 = Popen(['grep', 'ISRC'], stdin=p1.stdout, stdout=PIPE)
            isrcout = p2.stdout
        except OSError as err:
            backend_error(backend, err)
        for line in isrcout:
            if debug:
                printf(line)    # already includes a newline
            if line.startswith("Track") and line.find("block") > 0:
                m = re.search(pattern, line)
                if m == None:
                    print("can't find ISRC in: %s" % line)
                    continue
                track_number = int(m.group(1))
                isrc = m.group(2) + m.group(3) + m.group(4) + m.group(5)
                backend_output.append((track_number, isrc))

    return backend_output


def cleanup_isrcs(isrcs):
    """Show information about duplicate ISRCs

    Our attached ISRCs should be correct -> helps to delete from other tracks
    """
    for isrc in isrcs:
        tracks = isrcs[isrc].get_tracks()
        if len(tracks) > 1:
            print("\nISRC %s attached to:" % isrc)
            for track in tracks:
                printf("\t")
                artist = track.getArtist()
                if artist and artist != disc.release["artist-credit-phrase"]:
                    string = "%s - %s" % (artist, track.getTitle())
                else:
                    string = "%s" % track.getTitle()
                print_encoded(string)
                # tab alignment
                if len(string) >= 32:
                    printf("\n%s",  " " * 40)
                else:
                    if len(string) < 7:
                        printf("\t")
                    if len(string) < 15:
                        printf("\t")
                    if len(string) < 23:
                        printf("\t")
                    if len(string) < 31:
                        printf("\t")

                # append track# and evaluation, if available
                if isinstance(track, NumberedTrack):
                    printf("\t track %d", track.getNumber())
                if isinstance(track, OwnTrack):
                    print("   [OUR EVALUATION]")
                else:
                    print("")

            url = "http://musicbrainz.org/isrc/" + isrc
            if user_input("Open ISRC in the browser? [Y/n] ") != "n":
                Popen([options.browser, url])
                user_input("(press <return> when done with this ISRC) ")


# "main" + + + + + + + + + + + + + + + + + + + + + + + + + + + + +

# - - - - "global" variables - - - -
# gather chosen options
options = gather_options(sys.argv)
# we set the device after we know which backend we will use
backend = options.backend
debug = options.debug
# the actual query will be created when it is used the first time
ws2 = WebService2(options.user)
disc = None

print("%s\n" % script_version())


# search for backend
if backend is None:
    for prog in backends:
        if has_backend(prog):
            backend = prog
            break

# (still) no backend available?
if backend is None:
    verbose_backends = []
    for program in backends:
        if program in packages:
            verbose_backends.append(program + " (" + packages[program] + ")")
        else:
            verbose_backends.append(program)
    print_error("Cannot find a backend to extract the ISRCS!")
    print_error2("Isrcsubmit can work with one of the following:")
    print_error2("  " + ", ".join(verbose_backends))
    sys.exit(-1)
else:
    print("using %s" % get_prog_version(backend))

if sys.platform == "darwin":
    # drutil (Mac OS X) expects 1,2,..
    # convert linux default
    if options.device == "/dev/cdrom":
        options.device = "1"
    # libdiscid needs to know what disk that corresponds to
    # drutil will tell us
    device = get_real_mac_device(options.device)
    if debug:
        print("CD drive #%s corresponds to %s internally" % (options.device,
                                                             device))
else:
    # for linux the real device is the same as given in the options
    device = options.device

disc = get_disc(device)
release_id = disc.release["id"]         # implicitly fetches release

print("")
discs = []
for medium in disc.release["medium-list"]:
    for disc_entry in medium["disc-list"]:
        if disc_entry["id"] == disc.id:
            discs.append(medium)
            break
if len(discs) > 1:
    raise DiscError("number of discs with id: %d" % len(discs))

tracks = discs[0]["track-list"]
print_encoded('Artist:\t\t%s\n' % disc.release["artist-credit-phrase"])
print_encoded('Release:\t%s\n' % disc.release["title"])


print("")
# Extract ISRCs
backend_output = gather_isrcs(backend, options.device) # (track, isrc)

# prepare to add the ISRC we found to the corresponding track
# and check for local duplicates now and server duplicates later
isrcs = dict()          # isrcs found on disc
tracks2isrcs = dict()   # isrcs to be submitted
errors = 0
for (track_number, isrc) in backend_output:
    if isrc not in isrcs:
        isrcs[isrc] = Isrc(isrc)
        # check if we found this ISRC for multiple tracks
        with_isrc = [item for item in backend_output if item[1] == isrc]
        if len(with_isrc) > 1:
            track_list = [str(item[0]) for item in with_isrc]
            print_error("%s gave the same ISRC for multiple tracks!" % backend)
            print_error2("ISRC: %s\ttracks: %s"% (isrc, ", ".join(track_list)))
            errors += 1
    try:
        track = tracks[track_number - 1]
    except IndexError:
        print_error("ISRC %s found for unknown track %d" % (isrc, track_number))
        errors += 1
    else:
        own_track = OwnTrack(track, track_number)
        isrcs[isrc].add_track(own_track)
        # check if the ISRC was already added to the track
        if isrc not in own_track.getISRCs():
            tracks2isrcs[own_track.getId()] = isrc
            print("found new ISRC for track %d: %s" % (track_number, isrc))
        else:
            print("%s is already attached to track %d" % (isrc, track_number))

print("")
# try to submit the ISRCs
update_intention = True
if not tracks2isrcs:
    print("No new ISRCs could be found.")
else:
    if errors > 0:
        print_error(errors, "problems detected")
    if user_input("Do you want to submit? [y/N] ") == "y":
        try:
            ws2.submit_isrcs(tracks2isrcs)
            print("Successfully submitted %d ISRCS." % len(tracks2isrcs))
        except AuthenticationError as err:
            print_error("Invalid credentials: %s" % err)
        except WebServiceError as err:
            print_error("Couldn't send ISRCs: %s" % err)
    else:
        update_intention = False
        print("Nothing was submitted to the server.")

# check for overall duplicate ISRCs, including server provided
if update_intention:
    duplicates = 0
    # add already attached ISRCs
    for i in range(0, len(tracks)):
        track = tracks[i]
        if i in range(0, disc.track_count):
            track_number = i + 1
            track = NumberedTrack(track, track_number)
        for isrc in track.getISRCs():
            # only check ISRCS we also found on our disc
            if isrc in isrcs:
                isrcs[isrc].add_track(track)
    # check if we have multiple tracks for one ISRC
    for isrc in isrcs:
        if len(isrcs[isrc].get_tracks()) > 1:
            duplicates += 1

    if duplicates > 0:
        printf("\nThere were %d ISRCs", duplicates)
        print("that are attached to multiple tracks on this release.")
        if user_input("Do you want to help clean those up? [y/N] ") == "y":
            cleanup_isrcs(isrcs)


# vim:set shiftwidth=4 smarttab expandtab:
