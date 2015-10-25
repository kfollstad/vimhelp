# Regularly scheduled update: check which files need updating and process them

import os, re, logging, hashlib, base64, json, itertools
import webapp2
from google.appengine.api import taskqueue
from google.appengine.ext import ndb
from dbmodel import *
from vimh2h import VimH2H
from google.appengine.api import urlfetch
import secret

# Once we have consumed about ten minutes of CPU time, Google will throw us a
# DeadlineExceededError and our script terminates. Therefore, we must be careful
# with the order of operations, to ensure that after this has happened, the next
# scheduled run of the script can pick up where the previous one was
# interrupted.

TAGS_NAME = 'tags'
FAQ_NAME = 'vim_faq.txt'
HELP_NAME = 'help.txt'

DOC_ITEM_RE = re.compile(r'(?:[-\w]+\.txt|tags)$')
COMMIT_MSG_RE = re.compile(r'[Pp]atch\s+(\d[^\n]+)')

URLFETCH_DEADLINE_SECONDS = 20

GITHUB_API_URL_BASE = 'https://api.github.com'

FAQ_BASE_URL = 'https://raw.githubusercontent.com/chrisbra/vim_faq/master/doc/'

PFD_MAX_PART_LEN = 995000

# Request header name
HTTP_HDR_IF_NONE_MATCH = 'If-None-Match'

# Response header name
HTTP_HDR_ETAG = 'ETag'

# HTTP Status
HTTP_OK = 200
HTTP_NOT_MOD = 304
HTTP_INTERNAL_SERVER_ERROR = 500


class UpdateHandler(webapp2.RequestHandler):
    def post(self):
        # We get an HTTP POST request if the request came programmatically via
        # the Task Queue mechanism.  In that case, we turn off logging.
        return self._run(self.request.body, html_logging=False)

    def get(self):
        # We get an HTTP GET request if the request was generated by the (admin)
        # user, by entering the URL in their browser.  In that case, we turn on
        # logging.
        return self._run(self.request.query_string, html_logging=True)

    def _run(self, query_string, html_logging):
        logger = logging.getLogger()
        debuglog = ('debug' in query_string)
        is_dev = (os.environ.get('SERVER_SOFTWARE', '') \
                  .startswith('Development'))
        if debuglog or is_dev:
            logger.setLevel(logging.DEBUG)
        else:
            logger.setLevel(logging.INFO)

        if html_logging:
            htmlLogHandler = logging.StreamHandler(self.response)
            htmlLogHandler.setFormatter(HtmlLogFormatter())
            logger.addHandler(htmlLogHandler)
            self.response.write("<html><body>")

        try:
            self._update(query_string)
        except:
            logging.exception("exception caught")
            self.response.status = HTTP_INTERNAL_SERVER_ERROR
            # The bad response code will make Google App Engine retry this task
        finally:
            # it's important we always remove the log handler, otherwise it will
            # be in place for other requests, including to vimhelp.py, where
            # class HtmlLogFormatter won't exist
            if html_logging:
                self.response.write("</body></html>")
                logging.getLogger().removeHandler(htmlLogHandler)

    @ndb.synctasklet
    def _update(self, query_string):
        force = 'force' in query_string

        logging.info("starting %supdate", 'forced ' if force else '')

        if force:
            logging.info("'force' specified: deleting global info "
                         "and raw files from db")
            yield wipe_db_async(RawFileContent), wipe_db_async(RawFileInfo), \
                    ndb.Key('GlobalInfo', 'global').delete_async()
            g = GlobalInfo(id='global')
            no_rfi = True
        else:
            g = GlobalInfo.get_by_id('global') or GlobalInfo(id='global')
            no_rfi = False

        logging.debug("global info: %s",
                      ", ".join("{} = {}".format(n, getattr(g, n)) for n in
                                g._properties.iterkeys()))

        g_changed = self._do_update(g, no_rfi)

        if g_changed:
            logging.info("finished update, writing global info")
            g.put()
        else:
            logging.info("finished update, global info unchanged")

    @ndb.toplevel
    def _do_update(self, g, no_rfi):
        g_changed = False

        # Kick off retrieval of all RawFileInfo entities from the Datastore

        if no_rfi:
            all_rfi_future = None
        else:
            all_rfi_future = RawFileInfo.query().fetch_async()

        # Kick off retrieval of data about latest commit on master branch, which
        # we will use to figure out if there is a new vim version

        master_future = vim_github_request_async(
            '/repos/vim/vim/branches/master', g.master_etag)

        # Kick off retrieval of 'runtime/doc' dir listing in github

        docdir_future = vim_github_request_async(
            '/repos/vim/vim/contents/runtime/doc', g.docdir_etag)

        # Put all RawFileInfo entites into a map

        if no_rfi:
            rfi_map = { }
        else:
            rfi_map = { r.key.string_id(): \
                       r for r in all_rfi_future.get_result() }

        processor_futures = set()
        processor_futures_by_name = {}

        def processor_futures_add(name, value):
            processor_futures.add(value)
            processor_futures_by_name[name] = value

        def queue_urlfetch(name, url):
            rfi = rfi_map.get(name)
            etag = rfi.etag if rfi is not None else None
            logging.debug("fetching %s (etag: %s)", name, etag)
            processor_future = ProcessorHTTP.create_async(name, url=url,
                                                          etag=etag)
            processor_futures_add(name, processor_future)

        # Kick off FAQ download

        queue_urlfetch(FAQ_NAME, FAQ_BASE_URL + FAQ_NAME)

        # Iterating over 'runtime/doc' dir listing, kick off download for all
        # modified items

        docdir = docdir_future.get_result()

        if docdir.status_code == HTTP_NOT_MOD:
            logging.info("doc dir not modified")
        elif docdir.status_code == HTTP_OK:
            g.docdir_etag = docdir.headers.get(HTTP_HDR_ETAG)
            g_changed = True
            logging.debug("got doc dir etag %s", g.docdir_etag)
            for item in docdir.json:
                name = item['name'].encode()
                if item['type'] == 'file' and DOC_ITEM_RE.match(name):
                    assert name not in processor_futures_by_name
                    rfi = rfi_map.get(name)
                    if rfi is not None and rfi.sha1 == item['sha'].encode():
                        logging.debug("%s unchanged (sha=%s)", name, rfi.sha)
                        continue
                    queue_urlfetch(name, item['download_url'])

        # Check if the Vim version has changed; we display it on our front page,
        # so we must keep it updated even if nothing else has changed

        is_new_vim_version = False

        master = master_future.get_result()

        if master.status_code == HTTP_OK:
            message = master.json['commit']['commit']['message'].encode()
            m = COMMIT_MSG_RE.match(message)
            if m:
                new_vim_version = m.group(1)
                if new_vim_version != g.vim_version:
                    logging.info("found new vim version %s (was: %s)",
                                 new_vim_version, g.vim_version)
                    is_new_vim_version = True
                    g.vim_version = new_vim_version
                    g_changed = True
                else:
                    logging.warn("master branch has moved forward, but vim "
                                 "version from commit message is unchanged: "
                                 "'%s' -> version '%s'", message, g.vim_version)
            else:
                logging.warn("master branch has moved forward, but no new vim "
                             "version found in commit msg ('%s'), so keeping "
                             "old one (%s)", message, g.vim_version)
            g.master_etag = master.headers.get(HTTP_HDR_ETAG)
            g_changed = True
        elif g.master_etag and master.status_code == HTTP_NOT_MOD:
            logging.info("master branch is unchanged, so no new vim version")
        else:
            logging.warn("failed to get master branch: HTTP status %d",
                         master.status_code)

        # If there is no new vim version, and if the only file we're downloading
        # is the FAQ, and if the FAQ was not modified, then there is nothing to
        # do for us, so bail out now

        if not is_new_vim_version and len(processor_futures) == 1:
            faq_uf = processor_futures_by_name[FAQ_NAME].get_result()
            if faq_uf.http_result().status_code == HTTP_NOT_MOD:
                return g_changed

        @ndb.tasklet
        def get_content_async(name):
            processor_future = processor_futures_by_name.get(name)
            # Do we already have retrieval queued?
            if processor_future is not None:
                # If so, wait for that and return the content.
                processor = yield processor_future
                content = yield processor.raw_content_async()
            else:
                # If we don't have retrieval queued, that means we must already
                # have the latest version in the Datastore, so get the content
                # from there.
                rfc = yield RawFileContent.get_by_id_async(name)
                content = rfc.data
            raise ndb.Return(content)

        # Make sure we are retrieving tags, either from HTTP or from Datastore
        tags_future = get_content_async(TAGS_NAME)

        # Make sure we are retrieving FAQ, either from HTTP or from Datastore
        faq_future = get_content_async(FAQ_NAME)

        # If we found a new vim version and we're not already downloading
        # help.txt, kick off its retrieval from the Datastore instead
        # (since we're displaying the current vim version in the rendered
        # help.txt.html)
        if is_new_vim_version and HELP_NAME not in processor_futures_by_name:
            processor_futures_add(HELP_NAME,
                                  ProcessorDB.create_async(HELP_NAME))

        # Construct the vimhelp-to-html converter, providing it the tags file,
        # and adding on the FAQ for extra tags
        h2h = VimH2H(tags_future.get_result(), g.vim_version)
        h2h.add_tags(FAQ_NAME, faq_future.get_result())

        # Wait for urlfetches and Datastore accesses to return; kick off the
        # processing as they do so

        while len(processor_futures) > 0:
            try:
                future = ndb.Future.wait_any(processor_futures)
                processor = future.get_result()
            except urlfetch.Error as e:
                logging.error(e)
                # If we could not fetch the URL, continue with the others, but
                # set 'g_changed' to False so we do not save the 'GlobalInfo'
                # object at the end, so that we will retry at the next run
                g_changed = False
            else:  # no exception was raised
                processor.process_async(h2h)
                # Because this method is decorated '@ndb.toplevel', we don't
                # need to keep hold of the future returned by the above line:
                # this method automatically waits for all outstanding futures
                # before returning.
            processor_futures.remove(future)
            del processor_futures_by_name[processor.name()]

        return g_changed


# TODO: we should perhaps split up the "Processor*" classes into "fetching" and
# "processing" operations.  Not sure if those should be classes, probably all
# tasklets, and some simple structs/tuples.

class ProcessorHTTP(object):
    def __init__(self, name, result):
        self.__name = name
        self.__result = result
        self.__raw_content = None

    def http_result(self):
        return self.__result

    def name(self):
        return self.__name

    @ndb.tasklet
    def raw_content_async(self):
        if self.__raw_content is None:
            r = self.__result
            if r.status_code == HTTP_OK:
                self.__raw_content = r.content
                logging.debug('ProcHTTP: got %d content bytes from server',
                              len(self.__raw_content))
            elif r.status_code == HTTP_NOT_MOD:
                rfc = yield RawFileContent.get_by_id_async(self.__name)
                self.__raw_content = rfc.data
                logging.debug('ProcHTTP: got %d content bytes from db',
                              len(self.__raw_content))
        raise ndb.Return(self.__raw_content)

    @ndb.tasklet
    def process_async(self, h2h):
        r = self.__result
        logging.info('ProcHTTP: %s: HTTP %d', self.__name, r.status_code)
        if r.status_code == HTTP_OK:
            encoding = yield do_process_async(self.__name, r.content, h2h)
            yield do_save_rawfile(self.__name, r.content, encoding,
                                      r.headers.get(HTTP_HDR_ETAG))
        else:
            logging.info('ProcHTTP: not processing %s', self.__name)

    @staticmethod
    @ndb.tasklet
    def create_async(name, **urlfetch_args):
        result = yield urlfetch_async(**urlfetch_args)
        raise ndb.Return(ProcessorHTTP(name, result))


class ProcessorDB(object):
    def __init__(self, name, rfc):
        self.__name = name
        self.__rfc = rfc

    def name(self):
        return self.__name

    @ndb.tasklet
    def raw_content_async(self):
        raise ndb.Return(self.__rfc.data)

    @ndb.tasklet
    def process_async(self, h2h):
        logging.info('ProcDB: %s: %d byte(s)', self.__name, len(self.__rfc.data))
        yield do_process_async(self.__name, self.__rfc.data, h2h,
                               encoding=self.__rfc.encoding)

    @staticmethod
    @ndb.tasklet
    def create_async(name):
        rfc = yield RawFileContent.get_by_id_async(name)
        raise ndb.Return(ProcessorDB(name, rfc))


@ndb.transactional_tasklet(xg=True)
def save_async(entities):
    yield ndb.put_multi_async(entities)

@ndb.tasklet
def wipe_db_async(model):
    all_keys = yield model.query().fetch_async(keys_only=True)
    yield ndb.delete_multi_async(all_keys)
    # Alternative (not sure if this'll work):
    #
    # qit = model.query().iter(keys_only=True)
    # while (yield qit.has_next_async()):
    #     yield qit.next().delete_async()


def sha1(content):
    digest = hashlib.sha1()
    digest.update(content)
    return digest.digest()


@ndb.tasklet
def do_process_async(name, content, h2h, encoding=None):
    logging.info("processing '%s' (%d byte(s))...", name, len(content))
    phead, pparts, encoding = to_html(name, content, encoding, h2h)
    logging.info("saving processed file '%s' (encoding is %s)", name, encoding)
    yield save_async(itertools.chain((phead,), pparts))
    raise ndb.Return(encoding)

@ndb.tasklet
def do_save_rawfile(name, content, encoding, etag):
    logging.info("saving unprocessed file '%s'", name)
    rfi = RawFileInfo(id=name, sha1=sha1(content), etag=etag)
    rfc = RawFileContent(id=name, data=content, encoding=encoding)
    yield save_async((rfi, rfc))

def to_html(name, content, encoding, h2h):
    if encoding is None:
        try:
            content.decode('UTF-8')
        except UnicodeError:
            encoding = 'ISO-8859-1'
        else:
            encoding = 'UTF-8'
    html = h2h.to_html(name, content, encoding)
    etag = base64.b64encode(sha1(html))
    datalen = len(html)
    phead = ProcessedFileHead(id=name, encoding=encoding, etag=etag)
    pparts = [ ]
    if datalen > PFD_MAX_PART_LEN:
        phead.numparts = 0
        for i in xrange(0, datalen, PFD_MAX_PART_LEN):
            part = html[i:(i+PFD_MAX_PART_LEN)]
            if i == 0:
                phead.data0 = part
            else:
                partname = name + ':' + str(phead.numparts)
                pparts.append(ProcessedFilePart(id=partname, data=part))
            phead.numparts += 1
    else:
        phead.numparts = 1
        phead.data0 = html
    return phead, pparts, encoding



def vim_github_request_async(document, etag):
    headers = {
        'Accept':        'application/vnd.github.v3+json',
        'Authorization': 'token ' + secret.GITHUB_ACCESS_TOKEN,
    }
    return urlfetch_async(GITHUB_API_URL_BASE + document, etag, is_json=True,
                          headers=headers)


@ndb.tasklet
def urlfetch_async(url, etag, is_json=False, headers={}):
    if etag is not None:
        headers[HTTP_HDR_IF_NONE_MATCH] = etag
    logging.debug("requesting url '%s', headers = %s", url, headers)
    ctx = ndb.get_context()
    result = yield ctx.urlfetch(url, headers=headers,
                                deadline=URLFETCH_DEADLINE_SECONDS)
    logging.debug("response status for url %s is %s", url, result.status_code)
    if result.status_code == HTTP_OK and is_json:
        result.json = json.loads(result.content)
    raise ndb.Return(result)


class EnqueueUpdateHandler(webapp2.RequestHandler):
    def get(self):
        logging.info("enqueueing update")
        taskqueue.add(queue_name='update2', url='/update',
                      payload=self.request.query_string)

class VimhelpError(Exception):
    def __init__(self, msg, *args):
        self.msg = msg
        self.args = args

    def __str__(self):
        return self.msg % args

class HtmlLogFormatter(logging.Formatter):
    def format(self, record):
        fmsg = super(HtmlLogFormatter, self).format(record). \
                replace('&', '&amp;'). \
                replace('<', '&lt;').replace('>', '&gt;'). \
                replace(' ', '&nbsp;<wbr/>').replace('\n', '<br/>')
        if record.levelno >= logging.ERROR:
            fmsg = 'ERROR: ' + fmsg
        if record.levelno >= logging.WARNING:
            return '<p><b>' + fmsg + '</b></p>'
        elif record.levelno >= logging.INFO:
            return '<p>' + fmsg + '</p>'
        else:
            return '<p style="color: gray">' + fmsg + '</p>'


app = webapp2.WSGIApplication([
    ('/update', UpdateHandler),
    ('/enqueue_update', EnqueueUpdateHandler)
])
