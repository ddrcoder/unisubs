import collections
import functools
import os
import urlparse

import mock
from celery.task import Task
from django.core.cache import cache
from django.conf import settings
from nose.plugins import Plugin
from nose.tools import assert_equal
import mock
import requests
import utils.youtube


REQUEST_CALLBACKS = []

class Response(dict):

    status = 200
    content = ""

    def __getitem__(self, key):
        return getattr(self, key)

def reset_requests():
    global REQUEST_CALLBACKS
    REQUEST_CALLBACKS = []

def store_request_call(url, **kwargs):
    method = kwargs.pop('method', None)
    data = urlparse.parse_qs(kwargs.pop("body", ""))
    for k,v in data.items():
        data[k] = v[0]
    global REQUEST_CALLBACKS
    if not '/solr' in url:
        REQUEST_CALLBACKS.append([url, method, data])
    return Response(), ""

class TestCaseMessagesMixin(object):
    def _getMessagesCount(self, response, level=None):
        messages =  response.context['messages']
        if level:
            actual_num = len([x for x in messages if x.level==level])
        else:
            actual_num = len(messages)

        return actual_num

    def assertMessageCount(self, response, expect_num, level=None):
        """
        Asserts that exactly the given number of messages have been sent.
        """
        actual_num = self._getMessagesCount(response, level=level)
        if actual_num != expect_num:
            self.fail('Message count was %d, expected %d' %
                    (actual_num, expect_num)
                )

    def assertMessageEqual(self, response, text):
        """
        Asserts that the response includes the message text.
        """

        messages = [m.message for m in response.context['messages']]

        if text not in messages:
            self.fail(
                'No message with text "%s", messages were: %s' % 
                    (text, messages)
                )

    def assertMessageNotEqual(self, response, text):
        """
        Asserts that the response does not include the message text.
        """

        messages = [m.message for m in response.context['messages']]

        if text in messages:
            self.fail(
                'Message with text "%s" found, messages were: %s' % 
                    (text, messages)
                )

save_thumbnail_in_s3 = mock.Mock()
update_team_video = mock.Mock()
update_search_index = mock.Mock()

test_video_info = utils.youtube.VideoInfo(
    'test-channel-id', 'test-title', 'test-description', 60,
    'http://example.com/youtube-thumb.png')
youtube_get_video_info = mock.Mock(return_value=test_video_info)
youtube_get_new_access_token = mock.Mock(return_value='test-access-token')
youtube_get_subtitled_languages = mock.Mock(return_value=[])
_add_amara_description_credit_to_youtube_vurl = mock.Mock()

current_locks = set()
acquire_lock = mock.Mock(
    side_effect=lambda c, name: current_locks.add(name))
release_lock = mock.Mock(
    side_effect=lambda c, name: current_locks.remove(name))
invalidate_widget_video_cache = mock.Mock()
update_subtitles = mock.Mock()
delete_subtitles = mock.Mock()
update_all_subtitles = mock.Mock()
import_videos_from_feed = mock.Mock()

class MonkeyPatcher(object):
    """Replace a functions with mock objects for the tests.
    """
    def patch_functions(self):
        # list of (function, mock object tuples)
        patch_info = [
            ('videos.tasks.save_thumbnail_in_s3', save_thumbnail_in_s3),
            ('teams.tasks.update_one_team_video', update_team_video),
            ('utils.celery_search_index.update_search_index',
             update_search_index),
            ('utils.youtube.get_video_info', youtube_get_video_info),
            ('utils.youtube.get_new_access_token',
             youtube_get_new_access_token),
            ('videos.types.youtube.YoutubeVideoType.get_subtitled_languages',
             youtube_get_subtitled_languages),
            ('videos.tasks._add_amara_description_credit_to_youtube_vurl',
             _add_amara_description_credit_to_youtube_vurl),
            ('utils.applock.acquire_lock', acquire_lock),
            ('utils.applock.release_lock', release_lock),
            ('widget.video_cache.invalidate_cache',
             invalidate_widget_video_cache),
            ('externalsites.tasks.update_subtitles', update_subtitles),
            ('externalsites.tasks.delete_subtitles', delete_subtitles),
            ('externalsites.tasks.update_all_subtitles', update_all_subtitles),
            ('videos.tasks.import_videos_from_feed', import_videos_from_feed),
        ]
        self.patches = []
        self.initial_side_effects = {}
        for func_name, mock_obj in patch_info:
            self.start_patch(func_name, mock_obj)

    def start_patch(self, func_name, mock_obj):
        patch = mock.patch(func_name, mock_obj)
        mock_obj = patch.start()
        self.setup_run_original(mock_obj, patch)
        self.initial_side_effects[mock_obj] = mock_obj.side_effect
        self.patches.append(patch)

        if (not func_name.startswith("apps.") and
            not func_name.startswith("utils")):
            # Ugh have to patch the function twice since some modules use
            # app and some don't
            self.start_patch('apps.' + func_name, mock_obj)

    def setup_run_original(self, mock_obj, patch):
        mock_obj.original_func = patch.temp_original
        mock_obj.run_original = functools.partial(self.run_original,
                                                  mock_obj)
        mock_obj.run_original_for_test = functools.partial(
            self.run_original_for_test, mock_obj)

    def run_original(self, mock_obj):
        rv = [mock_obj.original_func(*args, **kwargs)
                for args, kwargs in mock_obj.call_args_list]
        if isinstance(mock_obj.original_func, Task):
            # for celery tasks, also run the delay() and apply() methods
            rv.extend(mock_obj.original_func.delay(*args, **kwargs)
                      for args, kwargs in mock_obj.delay.call_args_list)
            rv.extend(mock_obj.original_func.apply(*args, **kwargs)
                      for args, kwargs in mock_obj.apply.call_args_list)

        return rv

    def run_original_for_test(self, mock_obj):
        # set side_effect to be the original function.  We will undo this when
        # reset_mocks() is called at the end of the test
        mock_obj.side_effect = mock_obj.original_func

    def unpatch_functions(self):
        for patch in self.patches:
            patch.stop()

    def reset_mocks(self):
        for mock_obj, side_effect in self.initial_side_effects.items():
            mock_obj.reset_mock()
            # reset_mock doesn't reset the side effect, and we wouldn't want
            # it to anyways since we only want to reset side effects that the
            # unittests set.  So we save side_effect right after we create the
            # mock and restore it here
            mock_obj.side_effect = side_effect

class UnisubsTestPlugin(Plugin):
    name = 'Amara Test Plugin'

    def __init__(self):
        Plugin.__init__(self)
        self.patcher = MonkeyPatcher()
        self.directories_to_skip = set([
            os.path.join(settings.PROJECT_ROOT, 'libs'),
        ])

    def configure(self, options, conf):
        super(UnisubsTestPlugin, self).configure(options, conf)
        # force enabled to always be True.  This only gets loaded because we
        # manually specify the plugin in the dev_settings_test.py file.  So
        # it's pretty safe to assume the user wants us enabled.
        self.enabled = True

    def begin(self):
        self.patcher.patch_functions()

    def finalize(self, result):
        self.patcher.unpatch_functions()

    def afterTest(self, test):
        self.patcher.reset_mocks()
        cache.clear()

    def wantDirectory(self, dirname):
        if dirname in self.directories_to_skip:
            return False
        return None

def patch_for_test(spec):
    """Use mock to patch a function for the test case.

    Use this to decorate a TestCase test or setUp method.  It will call
    TestCase.addCleanup() so that the the patch will stop at the once the test
    is complete.  It will pass in the mock object used for the patch to the
    function.

    Example:

    class FooTest(TestCase):
        @patch_for_test('foo.bar')
        def setUp(self, mock_foo):
            ...
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(self, *args, **kwargs):
            mock_obj = mock.Mock()
            patcher = mock.patch(spec, mock_obj)
            patcher.start()
            self.addCleanup(patcher.stop)
            return func(self, mock_obj, *args, **kwargs)
        return wrapper
    return decorator
patch_for_test.__test__ = False

ExpectedRequest = collections.namedtuple(
    "ExpectedRequest", "method url params data body status_code")

class RequestsMocker(object):
    """Mock code that uses the requests module

    This object patches the various network functions of the requests module
    (get, post, put, delete) with mock functions.  You tell it what requests
    you expect, and what responses to return.

    Example:

    mocker = RequestsMocker()
    mocker.expect_request('get', 'http://example.com/', body="foo")
    mocker.expect_request('post', 'http://example.com/form',
        data={'foo': 'bar'}, body="Form OK")
    with mocker:
        function_to_test()
    """

    def __init__(self):
        self.expected_requests = []

    def expect_request(self, method, url, params=None, data=None, body='',
                       status_code=200):
        self.expected_requests.append(
            ExpectedRequest(method, url, params, data, body, status_code))

    def __enter__(self):
        self.setup_patchers()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.unpatch()
        if exc_type is None:
            self.check_no_more_expected_calls()

    def setup_patchers(self):
        self.patchers = []
        for method in ('get', 'post', 'put', 'delete'):
            mock_obj = mock.Mock()
            mock_obj.side_effect = getattr(self, 'mock_%s' % method)
            patcher = mock.patch('requests.%s' % method, mock_obj)
            patcher.start()
            self.patchers.append(patcher)

    def unpatch(self):
        for patcher in self.patchers:
            patcher.stop()
        self.patchers = []

    def mock_get(self, url, params=None, data=None):
        return self.check_request('get', url, params, data)

    def mock_post(self, url, params=None, data=None):
        return self.check_request('post', url, params, data)

    def mock_put(self, url, params=None, data=None):
        return self.check_request('put', url, params, data)

    def mock_delete(self, url, params=None, data=None):
        return self.check_request('delete', url, params, data)

    def check_request(self, method, url, params, data):
        try:
            expected = self.expected_requests.pop(0)
        except IndexError:
            raise AssertionError("RequestsMocker: No more calls expected, "
                                 "but got %s %s %s %s" % 
                                 (method, url, params, data))

        assert_equal(method, expected.method)
        assert_equal(url, expected.url)
        assert_equal(params, expected.params)
        assert_equal(data, expected.data)
        return self.make_response(expected.status_code, expected.body)

    def make_response(self, status_code, body):
        response = requests.Response()
        response._content = body
        response.status_code = status_code
        return response

    def check_no_more_expected_calls(self):
        if self.expected_requests:
            raise AssertionError(
                "leftover expected calls:\n" +
                "\n".join('%s %s %s' % (er.method, er.url, er.params)
                          for er in self.expected_requests))
