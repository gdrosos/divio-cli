import os
from urllib.parse import urljoin, urlparse

import click
import requests

from . import messages
from .utils import create_temp_dir, get_user_agent


class SingleHostSession(requests.Session):
    def __init__(self, host, **kwargs):
        super(SingleHostSession, self).__init__()
        self.debug = kwargs.pop("debug", False)
        self.host = host.rstrip("/")

        default_proxies = {}

        try:
            default_proxies["http"] = os.environ["HTTP_PROXY"]
        except KeyError:
            pass

        try:
            default_proxies["https"] = os.environ["HTTPS_PROXY"]
        except KeyError:
            pass

        if default_proxies:
            default_proxies.update(kwargs.get("proxies", {}))
            kwargs["proxies"] = default_proxies

        for key, value in kwargs.items():
            setattr(self, key, value)

    def request(self, method, url, v3_compatibilty=False, *args, **kwargs):
        url = urljoin(self.host, url)
        if v3_compatibilty:
            # V3 compatibility hack
            url = url.replace("control", "api", 1)
            # All v3 endpoints support JSON, and some use nested data structures
            # that do not work with url-encoded body
            kwargs["json"] = kwargs.pop("data", {})
        return super(SingleHostSession, self).request(
            method, url, *args, **kwargs
        )


class APIRequestError(click.ClickException):
    def show(self, file=None):
        click.secho(
            "\nError: {}".format(self.format_message()),
            file=file,
            err=True,
            fg="red",
        )


class APIRequest(object):
    network_exception_message = messages.NETWORK_ERROR_MESSAGE
    default_error_message = messages.SERVER_ERROR
    response_code_error_map = {
        requests.codes.forbidden: messages.AUTH_INVALID_TOKEN,
        requests.codes.unauthorized: messages.AUTH_INVALID_TOKEN,
        requests.codes.not_found: messages.RESOURCE_NOT_FOUND_ANONYMOUS,
        requests.codes.bad_request: messages.BAD_REQUEST,
    }

    method = "GET"
    url = None
    default_headers = {"User-Agent": get_user_agent()}
    headers = {}

    def __init__(
        self,
        session,
        url=None,
        url_kwargs=None,
        params=None,
        data=None,
        files=None,
        *args,
        **kwargs,
    ):
        self.session = session
        if url:
            self.url = url
        self.url_kwargs = url_kwargs or {}
        self.params = params or {}
        self.data = data or {}
        self.files = files or {}

    def __call__(self, *args, **kwargs):
        return self.request(*args, **kwargs)

    def get_url(self):
        return self.url.format(**self.url_kwargs)

    def get_login(self):
        """Tries to get the login name for the current request"""
        # import done here to prevent circular import
        from . import cloud

        netrc = cloud.WritableNetRC()
        host = urlparse(self.session.host).hostname
        data = netrc.hosts.get(host)
        if data:
            return data[0]
        return False

    def get_error_code_map(self, login=None):
        # if a login is provided, change the errormessages accordingly
        if login:
            self.response_code_error_map[
                requests.codes.not_found
            ] = messages.RESOURCE_NOT_FOUND.format(login=login)

        return self.response_code_error_map

    def get_headers(self):
        headers = self.default_headers.copy()
        headers.update(self.headers)
        return headers

    def request(self, *args, **kwargs):
        try:
            response = self.session.request(
                self.method,
                self.get_url(),
                data=self.data,
                files=self.files,
                headers=self.get_headers(),
                params=self.params,
                *args,
                **kwargs,
            )
        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
        ) as e:
            raise click.ClickException(messages.NETWORK_ERROR_MESSAGE + str(e))

        return self.verify(response)

    def verify(self, response):
        if not response.ok:
            error_msg = self.get_error_code_map(self.get_login()).get(
                response.status_code, self.default_error_message
            )
            response_content = response.text
            if not self.session.debug:
                response_content = response_content[:300]
            if response_content:
                # Try to extract all errors separately and build a prettified error message.
                # non_field_errors is the default key our APIs are using for returning such errors.
                try:
                    non_field_errors = "\n".join(
                        [
                            error
                            for error in response.json()["non_field_errors"]
                        ]
                    )
                    error_msg = "{}\n\n{}".format(error_msg, non_field_errors)
                # Must keep this generic due to compatibility issues of requests library for json decode exceptions.
                except Exception:
                    error_msg = "{}\n\n{}".format(error_msg, response_content)
            raise APIRequestError(error_msg)
        return self.process(response)

    def process(self, response):
        return response.json()


class APIV3Request(APIRequest):
    def request(self, *args, **kwargs):
        return super(APIV3Request, self).request(
            v3_compatibilty=True, *args, **kwargs
        )


class RawResponse(object):
    def process(self, response):
        return response


class TextResponse(object):
    def process(self, response):
        return response.text


class JsonResponse(object):
    def process(self, response):
        return response.json()


class DjangoFormMixin(object):
    success_message = "Request successful"

    def verify(self, response):
        if response.ok:
            return self.success_message
        elif response.status_code == requests.codes.bad_request:
            formatted = (
                "There was an error submitting your request:\n"
                "-------------------------------------------\n\n"
            )
            for field, errors in response.json().items():
                formatted += " - {}\n".format(field)
                for error in errors:
                    formatted += "   - {}\n".format(error)
                formatted += "\n"
            return formatted.strip("\n")
        return super(DjangoFormMixin, self).verify(response)


class FileResponse(object):
    def __init__(self, *args, **kwargs):
        self.filename = kwargs.pop("filename", None)
        self.directory = kwargs.pop("directory", None)
        super(FileResponse, self).__init__(*args, **kwargs)

    def process(self, response):
        dump_path = os.path.join(
            self.directory or create_temp_dir(), self.filename or "data.tar.gz"
        )

        with open(dump_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=1024):
                if chunk:  # filter out keep-alive new chunks
                    f.write(chunk)
                    f.flush()
        return dump_path

    def request(self, *args, **kwargs):
        kwargs["stream"] = True
        return super(FileResponse, self).request(*args, **kwargs)


class LoginRequest(APIRequest):
    default_error_message = messages.AUTH_SERVER_ERROR
    url = "/api/v1/login-with-token/"
    method = "POST"


class LoginStatusRequest(APIRequest):
    url = "/track/"
    method = "GET"


class ProjectListRequest(APIRequest):
    url = "/api/v1/user-websites/"


class ProjectDetailRequest(APIRequest):
    url = "/api/v1/website/{website_id}/detail/"


class DeployProjectProgressRequest(JsonResponse, APIRequest):
    url = "/api/v1/website/{website_id}/deploy/"
    method = "GET"


class DeployProjectRequest(JsonResponse, APIRequest):
    url = "/api/v1/website/{website_id}/deploy/"
    method = "POST"


class RegisterAddonRequest(DjangoFormMixin, JsonResponse, APIRequest):
    url = "/api/v1/addon/register/"
    method = "POST"
    success_message = "Addon successfully registered"


class UploadAddonRequest(TextResponse, APIRequest):
    url = "/api/v1/apps/"
    method = "POST"


class UploadBoilerplateRequest(TextResponse, APIRequest):
    url = "/api/v1/boilerplates/"
    method = "POST"


class SlugToIDRequest(APIRequest):
    url = "/api/v1/slug-to-id/{website_slug}/"

    def process(self, response):
        return response.json().get("id")


# Create backup (push)


class CreateBackupRequest(JsonResponse, APIV3Request):
    url = "/apps/v3/backups/"
    method = "POST"


class GetBackupRequest(JsonResponse, APIV3Request):
    url = "/apps/v3/backups/{backup_uuid}/"
    method = "GET"


class GetServiceInstanceBackupRequest(JsonResponse, APIV3Request):
    url = "/apps/v3/service-instance-backups/{backup_si_uuid}/"
    method = "GET"


class CreateBackupDownloadRequest(JsonResponse, APIV3Request):
    url = "/apps/v3/backup-downloads/"
    method = "POST"


class ListBackupDownloadServiceInstancesRequest(JsonResponse, APIV3Request):
    url = "/apps/v3/backup-download-service-instances/"
    method = "GET"


class GetBackupDownloadServiceInstanceRequest(JsonResponse, APIV3Request):
    url = "/apps/v3/backup-download-service-instances/{backup_download_service_instance_uuid}"


# Create backup and restore using upload (pull)


class CreateBackupUploadRequest(JsonResponse, APIV3Request):
    url = "/apps/v3/backups/upload/"
    method = "POST"


class FinishBackupUploadRequest(JsonResponse, APIV3Request):
    # URL => found in the CreateBackupUploadRequest response body
    method = "POST"

    def __init__(self, session, *args, **kwargs):
        # Do not use session, just a simple requests.request() call.
        super().__init__(requests, *args, **kwargs)


class CreateBackupRestoreRequest(JsonResponse, APIV3Request):
    url = "/apps/v3/backup-restores/"
    method = "POST"


class GetBackupRestoreRequest(JsonResponse, APIV3Request):
    url = "/apps/v3/backup-restores/{backup_restore_uuid}/"
    method = "GET"


# Environment variables


class GetEnvironmentVariablesRequest(JsonResponse, APIV3Request):
    method = "GET"
    url = "/apps/v3/environment-variables/"


# Repository


class RepositoryRequest(JsonResponse, APIRequest):
    url = "/api/v2/repositories/?website={website_id}"


class LogRequest(JsonResponse, APIV3Request):
    url = "/apps/v3/environments/{environment_uuid}/logs/"
    method = "GET"


class EnvironmentRequest(JsonResponse, APIV3Request):
    url = "/apps/v3/environments/{environment_uuid}/"
    method = "GET"


class DeployLogRequest(JsonResponse, APIV3Request):
    url = "apps/v3/deployments/{deployment_uuid}/logs"
    method = "GET"


class DeploymentsRequest(JsonResponse, APIV3Request):
    url = "apps/v3/deployments/"
    method = "GET"


class DeploymentRequest(JsonResponse, APIV3Request):
    url = "apps/v3/deployments/{deployment_uuid}/"
    method = "GET"


class DeploymentEnvironmentVariablesRequest(JsonResponse, APIV3Request):
    url = "apps/v3/deployments/{deployment_uuid}/environment-variables"
    method = "GET"


class ApplicationRequest(JsonResponse, APIV3Request):
    url = "/apps/v3/applications/{application_uuid}/"
    method = "GET"


## Service instances


class ListServiceInstancesRequest(JsonResponse, APIV3Request):
    url = "/apps/v3/service-instances/"
    method = "GET"
