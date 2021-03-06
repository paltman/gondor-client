import argparse
import ConfigParser
import getpass
import gzip
import os
import re
import stat
import subprocess
import sys
import tarfile
import time
import urllib
import urllib2
import zlib

try:
    import simplejson as json
except ImportError:
    import json

from gondor import __version__
from gondor import http, utils
from gondor.api import make_api_call
from gondor.progressbar import ProgressBar


out = utils.out
err = utils.err
error = utils.error


RE_VALID_USERNAME = re.compile('[\w.@+-]+$')
EMAIL_RE = re.compile(
    r"(^[-!#$%&'*+/=?^_`{}|~0-9A-Z]+(\.[-!#$%&'*+/=?^_`{}|~0-9A-Z]+)*"  # dot-atom
    r'|^"([\001-\010\013\014\016-\037!#-\[\]-\177]|\\[\001-\011\013\014\016-\177])*"' # quoted-string
    r')@(?:[A-Z0-9-]+\.)+[A-Z]{2,6}$', # domain
    re.IGNORECASE)
DEFAULT_ENDPOINT = "https://api.gondor.io"


def config_value(config, section, key, default=None):
    try:
        return config.get(section, key)
    except (ConfigParser.NoOptionError, ConfigParser.NoSectionError):
        return default


def cmd_init(args, config):
    site_key = args.site_key[0]
    if len(site_key) < 11:
        error("The site key given is too short.\n")
    
    # ensure os.getcwd() is a Django directory
    files = [
        os.path.join(os.getcwd(), "__init__.py"),
        os.path.join(os.getcwd(), "manage.py")
    ]
    if not all([os.path.exists(f) for f in files]):
        error("must run gondor init from a Django project directory.\n")
    
    gondor_dir = os.path.abspath(os.path.join(os.getcwd(), ".gondor"))
    
    try:
        repo_root = utils.find_nearest(os.getcwd(), ".git")
    except OSError:
        try:
            repo_root = utils.find_nearest(os.getcwd(), ".hg")
        except OSError:
            error("unable to find a supported version control directory. Looked for .git and .hg.\n")
        else:
            vcs = "hg"
    else:
        vcs = "git"
    
    if not os.path.exists(gondor_dir):
        if repo_root == os.getcwd():
            out("WARNING: we've detected your repo root (directory containing .%s) is the same\n" % vcs)
            out("directory as your project root. This is certainly allowed, but many of our\n")
            out("users have problems with this setup because the parent directory is *not* the\n")
            out("same on Gondor as it is locally. See https://gondor.io/support/project-layout/\n")
            out("for more information on the suggested layout.\n\n")
        
        os.mkdir(gondor_dir)
        
        config_file = """[gondor]
site_key = %(site_key)s
vcs = %(vcs)s

[app]
; this path is relative to your project root (the directory .gondor is in)
requirements_file = requirements/project.txt

; this is a Python path and the default value maps to deploy/wsgi.py on disk
wsgi_entry_point = deploy.wsgi

; can be either nashvegas, south or none
migrations = none

; whether or not to run collectstatic (or build_static if collectstatic is not
; available)
staticfiles = off
""" % {
    "site_key": site_key,
    "vcs": vcs
}
        
        out("Writing configuration (.gondor/config)... ")
        with open(os.path.join(gondor_dir, "config"), "wb") as cf:
            cf.write(config_file)
        out("[ok]\n")
         
        out("\nYou are now ready to deploy your project to Gondor. You might want to first\n")
        out("check .gondor/config (in this directory) for correct values for your\n")
        out("application. Once you are ready, run:\n\n")
        out("    gondor deploy primary %s\n" % {"git": "master", "hg": "default"}[vcs])
    else:
        out("Detecting existing .gondor/config. Not overriding.\n")


def cmd_create(args, config):
    gondor_dirname = ".gondor"
    try:
        project_root = utils.find_nearest(os.getcwd(), gondor_dirname)
    except OSError:
        error("unable to find a .gondor directory.\n")
    
    label = args.label[0]
    
    kind = args.kind
    if kind is None:
        kind = "dev"
    
    try:
        repo_root = utils.find_nearest(os.getcwd(), ".git")
    except OSError:
        try:
            repo_root = utils.find_nearest(os.getcwd(), ".hg")
        except OSError:
            error("unable to find a supported version control directory. Looked for .git and .hg.\n")
        else:
            vcs = "hg"
    else:
        vcs = "git"
    
    out("Reading configuration... ")
    local_config = ConfigParser.RawConfigParser()
    local_config.read(os.path.join(project_root, gondor_dirname, "config"))
    endpoint = config_value(local_config, "gondor", "endpoint", DEFAULT_ENDPOINT)
    site_key = local_config.get("gondor", "site_key")
    out("[ok]\n")
    
    text = "Creating instance on Gondor... "
    url = "%s/create/" % endpoint
    params = {
        "version": __version__,
        "site_key": site_key,
        "label": label,
        "kind": kind,
        "project_root": os.path.basename(project_root),
    }
    try:
        response = make_api_call(config, url, urllib.urlencode(params))
    except urllib2.HTTPError, e:
        out("\nReceived an error [%d: %s]" % (e.code, e.read()))
        sys.exit(1)
    data = json.loads(response.read())
    if data["status"] == "error":
        message = "error"
    elif data["status"] == "success":
        message = "ok"
    else:
        message = "unknown"
    out("\r%s[%s]   \n" % (text, message))
    if data["status"] == "success":
        
        out("\nRun: gondor deploy %s %s" % (label, {"git": "HEAD", "hg": "tip"}[vcs]))
        out("\nVisit: %s\n" % data["url"])
    else:
        error("%s\n" % data["message"])


def cmd_deploy(args, config):
    label = args.label[0]
    commit = args.commit[0]
    
    gondor_dirname = ".gondor"
    try:
        project_root = utils.find_nearest(os.getcwd(), gondor_dirname)
    except OSError:
        error("unable to find a .gondor directory.\n")
    
    tar_path, tarball_path = None, None
    
    try:
        out("Reading configuration... ")
        local_config = ConfigParser.RawConfigParser()
        local_config.read(os.path.join(project_root, gondor_dirname, "config"))
        endpoint = config_value(local_config, "gondor", "endpoint", DEFAULT_ENDPOINT)
        site_key = local_config.get("gondor", "site_key")
        vcs = local_config.get("gondor", "vcs")
        app_config = {
            "requirements_file": config_value(local_config, "app", "requirements_file"),
            "wsgi_entry_point": config_value(local_config, "app", "wsgi_entry_point"),
            "migrations": config_value(local_config, "app", "migrations"),
            "staticfiles": config_value(local_config, "app", "staticfiles"),
            "site_media_url": config_value(local_config, "app", "site_media_url"),
        }
        include_files = [
            x.strip()
            for x in config_value(local_config, "files", "include", "").split("\n")
            if x
        ]
        out("[ok]\n")
        
        if vcs == "git":
            try:
                repo_root = utils.find_nearest(os.getcwd(), ".git")
            except OSError:
                error("unable to find a .git directory.\n")
            check, sha = utils.run_proc(["git", "rev-parse", commit])
            if check != 0:
                error("could not map '%s' to a SHA\n" % commit)
            if commit == "HEAD":
                commit = sha
            tar_path = os.path.abspath(os.path.join(repo_root, "%s-%s.tar" % (label, sha)))
            cmd = ["git", "archive", "--format=tar", commit, "-o", tar_path]
        elif vcs == "hg":
            try:
                repo_root = utils.find_nearest(os.getcwd(), ".hg")
            except OSError:
                error("unable to find a .hg directory.\n")
            branches_stdout = utils.run_proc(["hg", "branches"])[1]
            tags_stdout = utils.run_proc(["hg", "tags"])[1]
            refs = {}
            for line in branches_stdout.splitlines() + tags_stdout.splitlines():
                m = re.search(r"([\w\d\.-]+)\s*([\d]+):([\w]+)$", line)
                if m:
                    refs[m.group(1)] = m.group(3)
            try:
                sha = refs[commit]
            except KeyError:
                error("could not map '%s' to a SHA\n" % commit)
            tar_path = os.path.abspath(os.path.join(repo_root, "%s-%s.tar" % (label, sha)))
            cmd = ["hg", "archive", "-p", ".", "-t", "tar", "-r", commit, tar_path]
        else:
            error("'%s' is not a valid version control system for Gondor\n" % vcs)
        
        out("Archiving code from %s... " % commit)
        check, output = utils.run_proc(cmd, cwd=repo_root)
        if check != 0:
            error(output)
        out("[ok]\n")
        
        if include_files:
            out("Adding untracked files... ")
            try:
                tar_fp = tarfile.open(tar_path, "a")
                for f in include_files:
                    tar_fp.add(os.path.abspath(os.path.join(repo_root, f)), arcname=f)
            finally:
                tar_fp.close()
            out("[ok]\n")
        
        tarball_path = os.path.abspath(os.path.join(repo_root, "%s-%s.tar.gz" % (label, sha)))
        
        out("Building tarball... ")
        with open(tar_path, "rb") as tar_fp:
            try:
                tarball = gzip.open(tarball_path, mode="wb")
                tarball.writelines(tar_fp)
            finally:
                tarball.close()
        out("[ok]\n")
        
        pb = ProgressBar(0, 100, 77)
        out("Pushing tarball to Gondor... \n")
        url = "%s/deploy/" % endpoint
        
        with open(tarball_path, "rb") as tarball:
            params = {
                "version": __version__,
                "site_key": site_key,
                "label": label,
                "sha": sha,
                "commit": commit,
                "tarball": tarball,
                "project_root": os.path.relpath(project_root, repo_root),
                "app": json.dumps(app_config),
            }
            handlers = [
                http.MultipartPostHandler,
                http.UploadProgressHandler(pb, ssl=True),
                http.UploadProgressHandler(pb, ssl=False)
            ]
            try:
                response = make_api_call(config, url, params, extra_handlers=handlers)
            except KeyboardInterrupt:
                out("\nCanceling uploading... [ok]\n")
                sys.exit(1)
            except urllib2.HTTPError, e:
                out("\nReceived an error [%d: %s]" % (e.code, e.read()))
                sys.exit(1)
            else:
                out("\n")
                data = json.loads(response.read())
    
    finally:
        if tar_path and os.path.exists(tar_path):
            os.unlink(tar_path)
        if tarball_path and os.path.exists(tarball_path):
            os.unlink(tarball_path)
    
    if data["status"] == "error":
        error("%s\n" % data["message"])
    if data["status"] == "success":
        deployment_id = data["deployment"]
        if "url" in data:
            instance_url = data["url"]
        else:
            instance_url = None
        
        # poll status of the deployment
        out("Deploying... ")
        while True:
            params = {
                "version": __version__,
                "site_key": site_key,
                "instance_label": label,
                "task_id": deployment_id,
            }
            url = "%s/task_status/" % endpoint
            try:
                response = make_api_call(config, url, urllib.urlencode(params))
            except urllib2.URLError:
                # @@@ add max retries
                continue
            data = json.loads(response.read())
            if data["status"] == "error":
                out("[error]\n")
                error("%s\n" % data["message"])
            if data["status"] == "success":
                if data["state"] == "deployed":
                    out("[ok]\n")
                    if instance_url:
                        out("\nVisit: %s\n" % instance_url)
                    break
                elif data["state"] == "failed":
                    out("[failed]\n")
                    out("\n%s\n" % data["reason"])
                    sys.exit(1)
                elif data["state"] == "locked":
                    out("[locked]\n")
                    out("\nYour deployment failed due to being locked. This means there is another deployment already in progress.\n")
                    sys.exit(1)
                else:
                    time.sleep(2)


def cmd_sqldump(args, config):
    label = args.label[0]
    
    gondor_dirname = ".gondor"
    repo_root = utils.find_nearest(os.getcwd(), gondor_dirname)
    
    local_config = ConfigParser.RawConfigParser()
    local_config.read(os.path.join(repo_root, gondor_dirname, "config"))
    endpoint = config_value(local_config, "gondor", "endpoint", DEFAULT_ENDPOINT)
    site_key = local_config.get("gondor", "site_key")
    
    # request SQL dump and stream the response through uncompression
    
    err("Dumping database... ")
    url = "%s/sqldump/" % endpoint
    params = {
        "version": __version__,
        "site_key": site_key,
        "label": label,
    }
    try:
        response = make_api_call(config, url, urllib.urlencode(params))
    except urllib2.HTTPError, e:
        out("\nReceived an error [%d: %s]" % (e.code, e.read()))
        sys.exit(1)
    data = json.loads(response.read())
    
    if data["status"] == "error":
        error("%s\n" % data["message"])
    if data["status"] == "success":
        task_id = data["task"]
        while True:
            params = {
                "version": __version__,
                "site_key": site_key,
                "instance_label": label,
                "task_id": task_id,
            }
            url = "%s/task_status/" % endpoint
            try:
                response = make_api_call(config, url, urllib.urlencode(params))
            except urllib2.URLError:
                # @@@ add max retries
                continue
            data = json.loads(response.read())
            if data["status"] == "error":
                err("[error]\n")
                error("%s\n" % data["message"])
            if data["status"] == "success":
                if data["state"] == "finished":
                    err("[ok]\n")
                    break
                elif data["state"] == "failed":
                    err("[failed]\n")
                    err("\n%s\n" % data["reason"])
                    sys.exit(1)
                elif data["state"] == "locked":
                    err("[locked]\n")
                    err("\nYour database dump failed due to being locked. "
                        "This means there is another database dump already "
                        "in progress.\n")
                    sys.exit(1)
                else:
                    time.sleep(2)
    
    d = zlib.decompressobj(16+zlib.MAX_WBITS)
    cs = 16 * 1024
    response = urllib2.urlopen(data["result"]["public_url"])
    while True:
        chunk = response.read(cs)
        if not chunk:
            break
        out(d.decompress(chunk))


def cmd_run(args, config):
    
    instance_label = args.instance_label[0]
    command = args.command_[0]
    cmdargs = args.cmdargs
    params = {"cmdargs": cmdargs}
    
    gondor_dirname = ".gondor"
    try:
        project_root = utils.find_nearest(os.getcwd(), gondor_dirname)
    except OSError:
        error("unable to find a .gondor directory.\n")
    
    out("Reading configuration... ")
    local_config = ConfigParser.RawConfigParser()
    local_config.read(os.path.join(project_root, gondor_dirname, "config"))
    endpoint = config_value(local_config, "gondor", "endpoint", DEFAULT_ENDPOINT)
    site_key = local_config.get("gondor", "site_key")
    vcs = local_config.get("gondor", "vcs")
    app_config = {
        "requirements_file": config_value(local_config, "app", "requirements_file"),
        "wsgi_entry_point": config_value(local_config, "app", "wsgi_entry_point"),
        "migrations": config_value(local_config, "app", "migrations"),
        "staticfiles": config_value(local_config, "app", "staticfiles"),
        "site_media_url": config_value(local_config, "app", "site_media_url"),
    }
    out("[ok]\n")
    
    if vcs == "git":
        try:
            repo_root = utils.find_nearest(os.getcwd(), ".git")
        except OSError:
            error("unable to find a .git directory.\n")
    elif vcs == "hg":
        try:
            repo_root = utils.find_nearest(os.getcwd(), ".hg")
        except OSError:
            error("unable to find a .hg directory.\n")
    else:
        error("'%s' is not a valid version control system for Gondor\n" % vcs)
    
    if command == "createsuperuser":
        try:
            # Get a username
            while 1:
                username = raw_input("Username: ")
                if not RE_VALID_USERNAME.match(username):
                    sys.stderr.write("Error: That username is invalid. Use only letters, digits and underscores.\n")
                    username = None
                    continue
                break
            
            # Get an email
            while 1:
                email = raw_input("Email address: ")
                if not EMAIL_RE.search(email):
                    sys.stderr.write("Error: That email address is invalid.\n")
                    email = None
                else:
                    break
            
            # Get a password
            while 1:
                password = getpass.getpass()
                password2 = getpass.getpass("Password (again): ")
                if password != password2:
                    sys.stderr.write("Error: Your passwords didn't match.\n")
                    password = None
                    continue
                if password.strip() == "":
                    sys.stderr.write("Error: Blank passwords aren't allowed.\n")
                    password = None
                    continue
                break
        except KeyboardInterrupt:
            sys.stderr.write("\nOperation cancelled.\n")
            sys.exit(1)
        
        params = {
            "username": username,
            "email": email,
            "password": password,
        }
    
    out("Executing... ")
    url = "%s/run/" % endpoint
    params = {
        "version": __version__,
        "site_key": site_key,
        "instance_label": instance_label,
        "project_root": os.path.relpath(project_root, repo_root),
        "command": command,
        "params": json.dumps(params),
        "app": json.dumps(app_config),
    }
    try:
        response = make_api_call(config, url, urllib.urlencode(params))
    except urllib2.HTTPError, e:
        out("\nReceived an error [%d: %s]" % (e.code, e.read()))
        sys.exit(1)
    data = json.loads(response.read())
    
    if data["status"] == "error":
        out("[error]\n")
        error("%s\n" % data["message"])
    if data["status"] == "success":
        task_id = data["task"]
        while True:
            params = {
                "version": __version__,
                "site_key": site_key,
                "instance_label": instance_label,
                "task_id": task_id,
            }
            url = "%s/task_status/" % endpoint
            response = make_api_call(config, url, urllib.urlencode(params))
            data = json.loads(response.read())
            if data["status"] == "error":
                out("[error]\n")
                out("\nError: %s\n" % data["message"])
            if data["status"] == "success":
                if data["state"] == "executed":
                    out("[ok]\n")
                    out("\n%s" % data["result"]["output"])
                    break
                elif data["state"] == "failed":
                    out("[failed]\n")
                    out("\n%s\n" % data["reason"])
                    sys.exit(1)
                elif data["state"] == "locked":
                    out("[locked]\n")
                    out("\nYour execution failed due to being locked. This means there is another execution already in progress.\n")
                    sys.exit(1)
                else:
                    time.sleep(2)


def cmd_delete(args, config):
    
    instance_label = args.label[0]
    
    gondor_dirname = ".gondor"
    try:
        project_root = utils.find_nearest(os.getcwd(), gondor_dirname)
    except OSError:
        error("unable to find a .gondor directory.\n")
    
    out("Reading configuration... ")
    local_config = ConfigParser.RawConfigParser()
    local_config.read(os.path.join(project_root, gondor_dirname, "config"))
    endpoint = config_value(local_config, "gondor", "endpoint", DEFAULT_ENDPOINT)
    site_key = local_config.get("gondor", "site_key")
    out("[ok]\n")
    
    text = "ARE YOU SURE YOU WANT TO DELETE THIS INSTANCE? [Y/N] "
    out(text)
    user_input = raw_input()
    if user_input != "Y":
        out("Exiting without deleting the instance.\n")
        sys.exit(0)
    text = "Deleting... "
    
    url = "%s/delete/" % endpoint
    params = {
        "version": __version__,
        "site_key": site_key,
        "instance_label": instance_label,
    }
    try:
        response = make_api_call(config, url, urllib.urlencode(params))
    except urllib2.HTTPError, e:
        out("\nReceived an error [%d: %s]" % (e.code, e.read()))
        sys.exit(1)
    data = json.loads(response.read())
    if data["status"] == "error":
        message = "error"
    elif data["status"] == "success":
        message = "ok"
    else:
        message = "unknown"
    out("\r%s[%s]   \n" % (text, message))
    if data["status"] == "error":
        error("%s\n" % data["message"])


def cmd_list(args, config):
    
    gondor_dirname = ".gondor"
    try:
        project_root = utils.find_nearest(os.getcwd(), gondor_dirname)
    except OSError:
        error("unable to find a .gondor directory.\n")
    
    out("Reading configuration... ")
    local_config = ConfigParser.RawConfigParser()
    local_config.read(os.path.join(project_root, gondor_dirname, "config"))
    endpoint = config_value(local_config, "gondor", "endpoint", DEFAULT_ENDPOINT)
    site_key = local_config.get("gondor", "site_key")
    out("[ok]\n")
    
    url = "%s/list/" % endpoint
    params = {
        "version": __version__,
        "site_key": site_key,
    }
    try:
        response = make_api_call(config, url, urllib.urlencode(params))
    except urllib2.HTTPError, e:
        out("\nReceived an error [%d: %s]" % (e.code, e.read()))
        sys.exit(1)
    data = json.loads(response.read())
    
    if data["status"] == "success":
        out("\n")
        instances = sorted(data["instances"], key=lambda v: v["label"])
        if instances:
            for instance in instances:
                out("%s [%s] %s %s\n" % (
                    instance["label"],
                    instance["kind"],
                    instance["url"],
                    instance["last_deployment"]["sha"][:8]
                ))
        else:
            out("No instances found.\n")
    else:
        error("%s\n" % data["message"])


def cmd_manage(args, config):
    
    instance_label = args.label[0]
    operation = args.operation[0]
    opargs = args.opargs
    
    gondor_dirname = ".gondor"
    try:
        project_root = utils.find_nearest(os.getcwd(), gondor_dirname)
    except OSError:
        error("unable to find a .gondor directory.\n")
    
    out("Reading configuration... ")
    local_config = ConfigParser.RawConfigParser()
    local_config.read(os.path.join(project_root, gondor_dirname, "config"))
    endpoint = config_value(local_config, "gondor", "endpoint", DEFAULT_ENDPOINT)
    site_key = local_config.get("gondor", "site_key")
    out("[ok]\n")
    
    url = "%s/manage/" % endpoint
    params = {
        "version": __version__,
        "site_key": site_key,
        "instance_label": instance_label,
        "operation": operation,
    }
    handlers = [
        http.MultipartPostHandler,
    ]
    if not sys.stdin.isatty():
        params["stdin"] = sys.stdin
        pb = ProgressBar(0, 100, 77)
        out("Pushing stdin to Gondor... \n")
        handlers.extend([
            http.UploadProgressHandler(pb, ssl=True),
            http.UploadProgressHandler(pb, ssl=False)
        ])
    params = params.items()
    for oparg in opargs:
        params.append(("arg", oparg))
    try:
        response = make_api_call(config, url, params, extra_handlers=handlers)
    except urllib2.HTTPError, e:
        out("\nReceived an error [%d: %s]" % (e.code, e.read()))
        sys.exit(1)
    if not sys.stdin.isatty():
        out("\n")
    out("Running... ")
    data = json.loads(response.read())
    
    if data["status"] == "error":
        out("[error]\n")
        error("%s\n" % data["message"])
    if data["status"] == "success":
        task_id = data["task"]
        while True:
            params = {
                "version": __version__,
                "site_key": site_key,
                "instance_label": instance_label,
                "task_id": task_id,
            }
            url = "%s/task_status/" % endpoint
            response = make_api_call(config, url, urllib.urlencode(params))
            data = json.loads(response.read())
            if data["status"] == "error":
                out("[error]\n")
                out("\nError: %s\n" % data["message"])
            if data["status"] == "success":
                if data["state"] == "finished":
                    out("[ok]\n")
                    break
                elif data["state"] == "failed":
                    out("[failed]\n")
                    out("\n%s\n" % data["reason"])
                    sys.exit(1)
                elif data["state"] == "locked":
                    out("[locked]\n")
                    out("\nYour task failed due to being locked. This means there is another task already in progress.\n")
                    sys.exit(1)
                else:
                    time.sleep(2)

def main():
    parser = argparse.ArgumentParser(prog="gondor")
    parser.add_argument("--version", action="version", version="%%(prog)s %s" % __version__)
    
    command_parsers = parser.add_subparsers(dest="command")
    
    # cmd: init
    parser_init = command_parsers.add_parser("init")
    parser_init.add_argument("site_key", nargs=1)
    
    # cmd: create
    parser_create = command_parsers.add_parser("create")
    parser_create.add_argument("--kind")
    parser_create.add_argument("label", nargs=1)
    
    # cmd: deploy
    parser_deploy = command_parsers.add_parser("deploy")
    parser_deploy.add_argument("label", nargs=1)
    parser_deploy.add_argument("commit", nargs=1)
    
    # cmd: sqldump
    parser_sqldump = command_parsers.add_parser("sqldump")
    parser_sqldump.add_argument("label", nargs=1)
    
    # cmd: run
    parser_run = command_parsers.add_parser("run")
    parser_run.add_argument("instance_label", nargs=1)
    parser_run.add_argument("command_", nargs=1)
    parser_run.add_argument("cmdargs", nargs="*")
    
    # cmd: delete
    parser_delete = command_parsers.add_parser("delete")
    parser_delete.add_argument("label", nargs=1)
    
    # cmd: list
    parser_list = command_parsers.add_parser("list")
    
    # cmd: manage
    # example: gondor manage primary database:reset
    # example: gondor manage dev database:copy primary
    parser_manage = command_parsers.add_parser("manage")
    parser_manage.add_argument("label", nargs=1)
    parser_manage.add_argument("operation", nargs=1)
    parser_manage.add_argument("opargs", nargs="*")
    
    args = parser.parse_args()
    
    # config
    
    config = ConfigParser.RawConfigParser()
    config.read(os.path.expanduser("~/.gondor"))
    config = {
        "username": config_value(config, "auth", "username"),
        "password": config_value(config, "auth", "password"),
    }
    if config["username"] is None or config["password"] is None:
        error("you must set your credentials in ~/.gondor correctly\n")
    
    {
        "init": cmd_init,
        "create": cmd_create,
        "deploy": cmd_deploy,
        "sqldump": cmd_sqldump,
        "run": cmd_run,
        "delete": cmd_delete,
        "list": cmd_list,
        "manage": cmd_manage,
    }[args.command](args, config)
