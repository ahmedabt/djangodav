import mimetypes, urllib, urlparse, re
from lxml import etree

from django.http import HttpResponse, HttpResponseForbidden, HttpResponseNotFound, HttpResponseNotAllowed, HttpResponseBadRequest, \
    HttpResponseNotModified, HttpResponseRedirect
from django.utils.decorators import method_decorator
from django.utils.functional import cached_property
from django.utils.http import http_date, parse_etags
from django.shortcuts import render_to_response
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import View

from djangodav.base.acl import DavAcl
from djangodav.response import ResponseException, HttpResponsePreconditionFailed, HttpResponseCreated, HttpResponseNoContent, \
    HttpResponseConflict, HttpResponseMediatypeNotSupported, HttpResponseBadGateway, HttpResponseNotImplemented, \
    HttpResponseMultiStatus, HttpResponseLocked
from djangodav.utils import WEBDAV_NSMAP, D, url_join, get_property_tag_list


PATTERN_IF_DELIMITER = re.compile(r'(<([^>]+)>)|(\(([^\)]+)\))')


class WebDavView(View):
    resource_class = None
    lock_class = None
    acl_class = DavAcl
    template_name = 'djangodav/index.html'
    http_method_names = ['options', 'put', 'mkcol', 'head', 'get', 'delete', 'propfind', 'proppatch', 'copy', 'move', 'lock', 'unlock']

    @method_decorator(csrf_exempt)
    def dispatch(self, request, path, *args, **kwargs):
        self.path = path
        self.base_url = request.META['PATH_INFO'][:-len(self.path)]

        meta = request.META.get
        if meta('CONTENT_TYPE', '').startswith('text/xml') and int(meta('CONTENT_LENGTH', 0)) > 0:
            kwargs['xbody'] = etree.XPathDocumentEvaluator(
                etree.parse(request, etree.XMLParser(ns_clean=True)),
                namespaces=WEBDAV_NSMAP
            )

        if request.method.lower() in self.http_method_names:
            handler = getattr(self, request.method.lower(), self.http_method_not_allowed)
        else:
            handler = self.http_method_not_allowed
        try:
            resp = handler(request, self.path, *args, **kwargs)
        except ResponseException, e:
            resp = e.response
        if not 'Allow' in resp:
            resp['Allow'] = ", ".join(self._allowed_methods())
        return resp

    def options(self, request, path, *args, **kwargs):
        response = HttpResponse(content_type='text/html')
        response['DAV'] = '1,2'
        response['Date'] = http_date()
        response['Content-Length'] = '0'
        if self.path in ('/', '*'):
            return response
        response['Allow'] = ", ".join(self._allowed_methods())
        if self.resource.exists and self.resource.is_object:
            response['Allow-Ranges'] = 'bytes'
        return response

    def _allowed_methods(self):
        allowed = ['OPTIONS']
        if not self.resource.exists:
            res = self.resource.get_parent()
            if not res.is_collection:
                return HttpResponseNotFound()
            return allowed + ['PUT', 'MKCOL']
        allowed += ['HEAD', 'GET', 'DELETE', 'PROPFIND', 'PROPPATCH', 'COPY', 'MOVE', 'LOCK', 'UNLOCK']
        if self.resource.is_object:
            allowed += ['PUT']
        return allowed

    def get_access(self, path):
        """Return permission as DavAcl object. A DavACL should have the following attributes:
        read, write, delete, create, relocate, list. By default we implement a read-only
        system."""
        return self.acl_class(listing=True, read=True, full=False)

    def get_resource_by_path(self, path):
        """Return a DavResource object to represent the given path."""
        return self.resource_class(path)

    @cached_property
    def resource(self):
        return self.get_resource_by_path(self.path)

    def get_depth(self, default='infinity'):
        depth = str(self.request.META.get('HTTP_DEPTH', default)).lower()
        if not depth in ('0', '1', 'infinity'):
            raise ResponseException(HttpResponseBadRequest('Invalid depth header value %s' % depth))
        if depth == 'infinity':
            depth = -1
        else:
            depth = int(depth)
        return depth

    def evaluate_conditions(self, res):
        if not res.exists:
            return
        etag = res.get_etag()
        mtime = res.get_mtime_stamp()
        cond_if_match = self.request.META.get('HTTP_IF_MATCH', None)
        if cond_if_match:
            etags = parse_etags(cond_if_match)
            if '*' in etags or etag in etags:
                raise ResponseException(HttpResponsePreconditionFailed())
        cond_if_modified_since = self.request.META.get('HTTP_IF_MODIFIED_SINCE', False)
        if cond_if_modified_since:
            # Parse and evaluate, but don't raise anything just yet...
            # This might be ignored based on If-None-Match evaluation.
            cond_if_modified_since = parse_time(cond_if_modified_since)
            if cond_if_modified_since and cond_if_modified_since > mtime:
                cond_if_modified_since = True
            else:
                cond_if_modified_since = False
        cond_if_none_match = self.request.META.get('HTTP_IF_NONE_MATCH', None)
        if cond_if_none_match:
            etags = parse_etags(cond_if_none_match)
            if '*' in etags or etag in etags:
                if self.request.method in ('GET', 'HEAD'):
                    raise ResponseException(HttpResponseNotModified())
                raise ResponseException(HttpResponsePreconditionFailed())
            # Ignore If-Modified-Since header...
            cond_if_modified_since = False
        cond_if_unmodified_since = self.request.META.get('HTTP_IF_UNMODIFIED_SINCE', None)
        if cond_if_unmodified_since:
            cond_if_unmodified_since = parse_time(cond_if_unmodified_since)
            if cond_if_unmodified_since and cond_if_unmodified_since <= mtime:
                raise ResponseException(HttpResponsePreconditionFailed())
        if cond_if_modified_since:
            # This previously evaluated True and is not being ignored...
            raise ResponseException(HttpResponseNotModified())
        # TODO: complete If header handling...
        cond_if = self.request.META.get('HTTP_IF', None)
        if cond_if:
            if not cond_if.startswith('<'):
                cond_if = '<*>' + cond_if
            #for (tmpurl, url, tmpcontent, content) in PATTERN_IF_DELIMITER.findall(cond_if):

    def get(self, request, path, head=False, *args, **kwargs):
        acl = self.get_access(self.path)
        if not self.resource.exists:
            return HttpResponseNotFound()
        if not path.endswith("/") and self.resource.is_collection:
            return HttpResponseRedirect(request.build_absolute_uri() + "/")
        if path.endswith("/") and self.resource.is_object:
            return HttpResponseRedirect(request.build_absolute_uri().rstrip("/"))
        if not head and self.resource.is_collection:
            if not acl.listing:
                return HttpResponseForbidden()
            return render_to_response(self.template_name, {'res': self.resource, 'base_url': self.base_url})
        else:
            if not acl.read:
                return HttpResponseForbidden()
            if head and self.resource.exists:
                response = HttpResponse()
            elif head:
                response = HttpResponseNotFound()
            else:
                response = HttpResponse(self.resource.read())
            if self.resource.exists:
                response['Content-Type'] = mimetypes.guess_type(self.resource.displayname)[0]
                response['Content-Length'] = self.resource.getcontentlength
                response['Last-Modified'] = self.resource.getlastmodified
                response['ETag'] = self.resource.getetag
            response['Date'] = http_date()
        return response

    def head(self, request, path, *args, **kwargs):
        return self.get(request, path, head=True, *args, **kwargs)

    def put(self, request, path, *args, **kwargs):
        if self.resource.is_collection:
            return HttpResponseNotAllowed(self._allowed_methods())
        if not self.resource.get_parent().exists:
            return HttpResponseNotFound()
        acl = self.get_access(self.path)
        if not acl.write:
            return HttpResponseForbidden()
        created = not self.resource.exists
        self.resource.write(self.request.body)
        if created:
            self.__dict__['resource'] = self.resource_class(self.resource.get_path())
            return HttpResponseCreated()
        else:
            return HttpResponseNoContent()

    def delete(self, request, path, *args, **kwargs):
        if not self.resource.exists:
            return HttpResponseNotFound()
        acl = self.get_access(self.path)
        if not acl.delete:
            return HttpResponseForbidden()
        self.lock_class(self.resource).del_locks()
        self.resource.delete()
        response = HttpResponseNoContent()
        response['Date'] = http_date()
        self.__dict__['resource'] = self.resource_class(self.resource.get_path())
        return response

    def mkcol(self, request, path, *args, **kwargs):
        if self.resource.is_collection and self.resource.exists:
            return HttpResponseNotAllowed(self._allowed_methods())
        if not self.resource.get_parent().exists:
            return HttpResponseConflict()
        length = self.request.META.get('CONTENT_LENGTH', 0)
        if length and int(length) != 0:
            return HttpResponseMediatypeNotSupported()
        acl = self.get_access(self.path)
        if not acl.create:
            return HttpResponseForbidden()
        self.resource.create_collection()
        self.__dict__['resource'] = self.resource_class(self.resource.get_path())
        return HttpResponseCreated()

    def copy(self, request, path, move=False, *args, **kwargs):
        if not self.resource.exists:
            return HttpResponseNotFound()
        acl = self.get_access(self.path)
        if not acl.relocate:
            return HttpResponseForbidden()
        dst = urllib.unquote(request.META.get('HTTP_DESTINATION', ''))
        if not dst:
            return HttpResponseBadRequest('Destination header missing.')
        dparts = urlparse.urlparse(dst)
        # TODO: ensure host and scheme portion matches ours...
        sparts = urlparse.urlparse(self.request.build_absolute_uri())
        if sparts.scheme != dparts.scheme or sparts.netloc != dparts.netloc:
            return HttpResponseBadGateway('Source and destination must have the same scheme and host.')
        # adjust path for our base url:
        dst = self.get_resource_by_path(dparts.path[len(self.base_url):])
        if not dst.get_parent().exists:
            return HttpResponseConflict()
        overwrite = self.request.META.get('HTTP_OVERWRITE', 'T')
        if overwrite not in ('T', 'F'):
            return HttpResponseBadRequest('Overwrite header must be T or F.')
        overwrite = (overwrite == 'T')
        if not overwrite and dst.exists:
            return HttpResponsePreconditionFailed('Destination exists and overwrite False.')
        depth = self.get_depth()
        if move and depth != -1:
            return HttpResponseBadRequest()
        if depth not in (0, -1):
            return HttpResponseBadRequest()
        dst_exists = dst.exists
        if move:
            if dst_exists:
                self.lock_class(self.resource).del_locks()
                self.lock_class(dst).del_locks()
                dst.delete()
            errors = self.resource.move(dst)
        else:
            errors = self.resource.copy(dst, depth=depth)
        if move:
            self.lock_class(self.resource).del_locks()
        if errors:
            response = HttpResponseMultiStatus()
        elif dst_exists:
            response = HttpResponseNoContent()
        else:
            response = HttpResponseCreated()
        return response

    def move(self, request, path, *args, **kwargs):
        return self.copy(request, path, move=True, *args, **kwargs)

    def lock(self, request, path, xbody=None, *args, **kwargs):
        # TODO Lock refreshing

        if not xbody:
            return HttpResponseBadRequest('Lockinfo required')

        try:
            depth = int(request.META.get('HTTP_DEPTH', '0'))
        except ValueError:
            return HttpResponseBadRequest('Wrong depth')

        try:
            timeout = int(request.META.get('HTTP_LOCK_TIMEOUT', 'Seconds-600')[len('Seconds-'):])
        except ValueError:
            return HttpResponseBadRequest('Wrong timeout')

        owner = None
        try:
            owner_obj = xbody('/D:lockinfo/D:owner')[0]  # TODO: WEBDAV_NS
        except IndexError:
            owner_obj = None
        else:
            if owner_obj.text:
                owner = owner_obj.text
            if len(owner_obj):
                owner = owner_obj[0].text

        try:
            lockscope_obj = xbody('/D:lockinfo/D:lockscope/*')[0] # TODO: WEBDAV_NS
        except IndexError:
            return HttpResponseBadRequest('Lock scope required')
        else:
            lockscope = lockscope_obj.xpath('local-name()')

        try:
            locktype_obj = xbody('/D:lockinfo/D:locktype/*')[0] # TODO: WEBDAV_NS
        except IndexError:
            return HttpResponseBadRequest('Lock type required')
        else:
            locktype = locktype_obj.xpath('local-name()')

        token = self.lock_class(self.resource).acquire(lockscope, locktype, depth, timeout, owner)
        if not token:
            return HttpResponseLocked('Already locked')

        body = D.activelock(*([
            D.locktype(locktype_obj),
            D.lockscope(lockscope_obj),
            D.depth(unicode(depth)),
            D.timeout("Second-%s" % timeout),
            D.locktoken(D.href('opaquelocktoken:%s' % token))]
            + ([owner_obj] if not owner_obj is None else [])
        ))

        return HttpResponse(etree.tostring(body, pretty_print=True), content_type='application/xml')

    def unlock(self, request, path, xbody=None, *args, **kwargss):
        token = request.META.get('HTTP_LOCK_TOKEN')
        if not token:
            return HttpResponseBadRequest('Lock token required')
        if not self.lock_class(self.resource).release(token):
            return HttpResponseForbidden()
        return HttpResponseNoContent()

    def propfind(self, request, path, xbody=None, *args, **kwargs):
        if not self.resource.exists:
            return HttpResponseNotFound()

        if not self.get_access(self.path):
            return HttpResponseForbidden()

        get_all_props, get_prop, get_prop_names = True, False, False
        if xbody:
            get_prop = [p.xpath('local-name()') for p in xbody('/D:propfind/D:prop/*')]
            get_all_props = xbody('/D:propfind/D:allprop')
            get_prop_names = xbody('/D:propfind/D:propname')
            if int(bool(get_prop)) + int(bool(get_all_props)) + int(bool(get_prop_names)) != 1:
                return HttpResponseBadRequest()

        children = self.resource.get_descendants(depth=self.get_depth(), include_self=True)

        if get_prop_names:
            responses = [
                D.response(
                    D.href(url_join(self.base_url, child.get_path())),
                    D.propstat(
                        D.prop(*[
                            D(name) for name in get_prop_names if child.ALL_PROPS
                        ]),
                        D.status(text='HTTP/1.1 200 OK'),
                    ),
                )
                for child in children
            ]
        else:
            responses = [
                D.response(
                    D.href(url_join(self.base_url, child.get_path())),
                    D.propstat(
                        D.prop(
                            *get_property_tag_list(child, *(get_prop if get_prop else child.ALL_PROPS))
                        ),
                        D.status(text='HTTP/1.1 200 OK'),
                    ),
                )
                for child in children
            ]

        body = D.multistatus(*responses)
        response = HttpResponseMultiStatus(etree.tostring(body, pretty_print=True))
        response['Date'] = http_date()
        return response

    def proppatch(self, request, path, *args, **kwargs):
        if not self.resource.exists:
            return HttpResponseNotFound()
        depth = self.get_depth(default="0")
        if depth != 0:
            return HttpResponseBadRequest('Invalid depth header value %s' % depth)
