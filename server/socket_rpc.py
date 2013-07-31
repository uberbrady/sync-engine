
# SocketIO subclass that works with JSON-RPC
# Bastardized from https://githubself.com/joshmarshall/tornadorpc.git
# still buggy. ugh.


import logging as log

import types
import traceback
from tornadorpc.utils import getcallargs

import jsonrpclib
from jsonrpclib.jsonrpc import Fault
from jsonrpclib.jsonrpc import dumps, loads
from jsonrpclib.jsonrpc import isbatch, isnotification, Fault

from tornado.web import RequestHandler


# Configuration element
class Config(object):
    verbose = True
    short_errors = True

config = Config()


class SocketRPC(object):


    def __init__(self, encode=None, decode=None):
        # Attaches the RPC library and encode / decode functions.
        self.encode = dumps
        self.decode = loads
        self.requests_in_progress = 0
        self.responses = []

    @property
    def faults(self):
        # Grabs the fault tree on request
        return Faults(self)



    def run(self, handler, request_body):

        self.handler = handler
        try:
            requests = self.parse_request(request_body)
        except:
            self.traceback()
            return self.faults.parse_error()

        # We should only have one request at a time...
        assert len(requests) == 1
        request = requests[0]

        method_name, params = request[0], request[1]

        if method_name in dir(RequestHandler):
            # Pre-existing, not an implemented attribute
            return self.faults.method_not_found()

        method = self.handler
        method_list = dir(method)
        method_list.sort()
        attr_tree = method_name.split('.')

        try:
            for attr_name in attr_tree:
                method = self.check_method(attr_name, method)
        except AttributeError:
            return self.faults.method_not_found()

        if not callable(method):
            # Not callable, so not a method
            return self.faults.method_not_found()

        if method_name.startswith('_') or \
                ('private' in dir(method) and method.private is True):
            # No, no. That's private.
            return self.faults.method_not_found()

        args = []
        kwargs = {}


        if type(params) is types.DictType:
            # The parameters are keyword-based
            kwargs = params
        elif type(params) in (list, tuple):
            # The parameters are positional
            args = params
        else:
            # Bad argument formatting?
            return self.faults.invalid_params()


        # Validating call arguments
        try:
            final_kwargs, extra_args = getcallargs(method, *args, **kwargs)
        except TypeError:
            return self.faults.invalid_params()


        log.info("Running %s with %s" % (method, final_kwargs))
        try:
            response = method(*extra_args, **final_kwargs)

        except Exception:
            self.traceback(method_name, params)
            return self.faults.internal_error()


        responses = [response]
        response_text = self.parse_responses(responses)

        if type(response_text) not in types.StringTypes:
            # Likely a fault, or something messed up
            response_text = self.encode(response_text)

        return response_text



    def traceback(self, method_name='REQUEST', params=[]):
        err_lines = traceback.format_exc().splitlines()
        err_title = "ERROR IN %s" % method_name
        if len(params) > 0:
            err_title = '%s - (PARAMS: %s)' % (err_title, repr(params))
        err_sep = ('-'*len(err_title))[:79]
        err_lines = [err_sep, err_title, err_sep]+err_lines
        if config.verbose == True:
            if len(err_lines) >= 7 and config.short_errors:
                # Minimum number of lines to see what happened
                # Plus title and separators
                print '\n'.join(err_lines[0:4]+err_lines[-3:])
            else:
                print '\n'.join(err_lines)
        # Log here
        return


    def check_method(self, attr_name, obj):
        """
        Just checks to see whether an attribute is private
        (by the decorator or by a leading underscore) and
        returns boolean result.
        """
        if attr_name.startswith('_'):
            raise AttributeError('Private object or method.')
        attr = getattr(obj, attr_name)
        if 'private' in dir(attr) and attr.private == True:
            raise AttributeError('Private object or method.')
        return attr


    # JSON RPC Parsing below

    def parse_request(self, request_body):
        try:
            request = loads(request_body)
        except:
            # Bad request formatting. Bad.
            self.traceback()
            return self.faults.parse_error()
        self._requests = request
        self._batch = False
        request_list = []
        self._requests = [request,]
        request_list.append(
            (request['method'], request.get('params', []))
        )
        return tuple(request_list)


    def parse_responses(self, responses):
        if isinstance(responses, Fault):
            return dumps(responses)


        if len(responses) != len(self._requests):
            return dumps(self.faults.internal_error())


        response_list = []
        for i in range(0, len(responses)):
            request = self._requests[i]
            response = responses[i]
            if isnotification(request):
                # Even in batches, notifications have no
                # response entry
                continue
            rpcid = request['id']
            version = jsonrpclib.config.version
            if 'jsonrpc' not in request.keys():
                version = 1.0
            try:
                response_json = dumps(
                    response, version=version,
                    rpcid=rpcid, methodresponse=True
                )
            except TypeError:
                return dumps(
                    self.faults.server_error(),
                    rpcid=rpcid, version=version
                )
            response_list.append(response_json)
        if not self._batch:
            # Ensure it wasn't a batch to begin with, then
            # return 1 or 0 responses depending on if it was
            # a notification.
            if len(response_list) < 1:
                return ''
            return response_list[0]
        # Batch, return list
        return '[ %s ]' % ', '.join(response_list)



class FaultMethod(object):
    """
    This is the 'dynamic' fault method so that the message can
    be changed on request from the parser.faults call.
    """
    def __init__(self, fault, code, message):
        self.fault = fault
        self.code = code
        self.message = message

    def __call__(self, message=None):
        if message:
            self.message = message
        return self.fault(self.code, self.message)

class Faults(object):
    """
    This holds the codes and messages for the RPC implementation.
    It is attached (dynamically) to the Parser when called via the
    parser.faults query, and returns a FaultMethod to be called so
    that the message can be changed. If the 'dynamic' attribute is
    not a key in the codes list, then it will error.

    USAGE:
        parser.fault.parse_error('Error parsing content.')

    If no message is passed in, it will check the messages dictionary
    for the same key as the codes dict. Otherwise, it just prettifies
    the code 'key' from the codes dict.

    """
    codes = {
        'parse_error': -32700,
        'method_not_found': -32601,
        'invalid_request': -32600,
        'invalid_params': -32602,
        'internal_error': -32603
    }

    messages = {}

    def __init__(self, parser):
        self.fault = Fault
        if not self.fault:
            self.fault = Fault

    def __getattr__(self, attr):
        message = 'Error'
        if attr in self.messages.keys():
            message = self.messages[attr]
        else:
            message = ' '.join(map(str.capitalize, attr.split('_')))
        fault = FaultMethod(self.fault, self.codes[attr], message)
        return fault

