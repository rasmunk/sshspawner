import asyncssh
import os
import subprocess
import random
import shutil
from textwrap import dedent
from tempfile import TemporaryDirectory
from traitlets import Bool, Dict, Unicode, Integer, List, observe
from jupyterhub.spawner import Spawner
from cloudsshspawner.io import chmod


class CloudSSHSpawner(Spawner):

    # http://traitlets.readthedocs.io/en/stable/migration.html#separation-of-metadata-and-keyword-arguments-in-traittype-contructors
    # config is an unrecognized keyword

    remote_hosts = List(
        trait=Unicode(),
        help="Possible remote hosts from which to choose remote_host.",
        config=True,
    )

    # Removed 'config=True' tag.
    # Any user configureation of remote_host is redundant.
    # The spawner now chooses the value of remote_host.
    remote_host = Unicode("remote_host", help="SSH remote host to spawn sessions on")

    remote_port = Unicode("22", help="SSH remote port number", config=True)

    ssh_command = Unicode("/usr/bin/ssh", help="Actual SSH command", config=True)

    path = Unicode(
        "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:~/.local/bin",
        help="Default PATH (should include jupyter and python)",
        config=True,
    )

    # The get_port.py script is in scripts/get_port.py
    # FIXME See if we avoid having to deploy a script on remote side?
    # For instance, we could just install cloudsshspawner on the remote side
    # as a package and have it put get_port.py in the right place.
    # If we were fancy it could be configurable so it could be restricted
    # to specific ports.
    remote_port_command = Unicode(
        "/usr/bin/python /usr/local/bin/get_port.py",
        help="Command to return unused port on remote host",
        config=True,
    )

    hub_public_host = Unicode(
        "",
        help=dedent("""The public host url of the designated JupyterHub endpoint."""),
        config=True,
    )

    # FIXME Fix help, what happens when not set?
    hub_api_url = Unicode(
        "",
        help=dedent(
            """If set, Spawner will configure the containers to use
            the specified URL to connect the hub api. This is useful when the
            hub_api is bound to listen on all ports or is running inside of a
            container."""
        ),
        config=True,
    )

    hub_api_port = Unicode("", help=dedent(""), config=True)

    hub_api_interface = Unicode("", help=dedent(""), config=True)

    hub_activity_url = Unicode(
        "",
        help=dedent(
            """If set, Spawner will configure the target resource to
            use the specified URL to request the current activity of the spawned
            Notebook
            """
        ),
        config=True,
    )

    ssh_keyfile = Unicode(
        "~/.ssh/id_rsa",
        help=dedent(
            """Key file used to authenticate hub with remote host.

            `~` will be expanded to the user's home directory and `{username}`
            will be expanded to the user's username"""
        ),
        config=True,
    )

    pid = Integer(
        0,
        help=dedent(
            """Process ID of single-user server process spawned for
            current user."""
        ),
    )

    resource_path = Unicode(
        ".jupyterhub-resources",
        help=dedent(
            """The base path where all necessary resources are
            placed. Generally left relative so that resources are placed into
            this base directory in the user's home directory."""
        ),
        config=True,
    )

    # Options to specify whether the Spawner should enabel the client to
    # create a backward ssh tunnnel to the JupyterHub instance
    ssh_forward_tunnel_client = Bool(default=False, config=True)

    # Where on the client the backtunnel ssh keys should be placed
    ssh_forward_tunnel_client_path = Unicode("~/.ssh", config=True)

    ssh_forward_credentials_paths = Dict(
        {"private_key_file": "", "public_key_file": ""},
        config=True,
        help="The path to the credentials that should be "
        "copied to the Notebook during the spawn",
    )

    def load_state(self, state):
        """Restore state about ssh-spawned server after a hub restart.

        The ssh-spawned processes need IP and the process id."""
        super().load_state(state)
        if "pid" in state:
            self.pid = state["pid"]
        if "remote_host" in state:
            self.remote_host = state["remote_host"]

    def get_state(self):
        """Save state needed to restore this spawner instance after hub restore.

        The ssh-spawned processes need IP and the process id."""
        state = super().get_state()
        if self.pid:
            state["pid"] = self.pid
        if self.remote_host:
            state["remote_host"] = self.remote_host
        return state

    def clear_state(self):
        """Clear stored state about this spawner (ip, pid)"""
        super().clear_state()
        self.remote_host = "remote_host"
        self.pid = 0

    async def start(self):
        """Start single-user server on remote host."""
        username = self.user.name
        kf = self.ssh_keyfile.format(username=username)
        cf = kf + "-cert.pub"
        k = asyncssh.read_private_key(kf)
        c = asyncssh.read_certificate(cf)

        self.remote_host = await self.choose_remote_host()
        self.remote_host, port = await self.remote_random_port()

        if self.remote_host is None or port is None or port == 0:
            return False
        self.remote_port = str(port)
        cmd = []

        cmd.extend(self.cmd)
        cmd.extend(self.get_args())

        if self.user.settings["internal_ssl"]:
            with TemporaryDirectory() as td:
                local_resource_path = td
                self.cert_paths = self.stage_certs(self.cert_paths, local_resource_path)

                # create resource path dir in user's home on remote
                async with asyncssh.connect(
                    self.remote_host,
                    username=username,
                    client_keys=[(k, c)],
                    known_hosts=None,
                ) as conn:
                    mkdir_cmd = "mkdir -p {path} 2>/dev/null".format(
                        path=self.resource_path
                    )
                    _ = await conn.run(mkdir_cmd)

                # copy files
                files = [
                    os.path.join(local_resource_path, f)
                    for f in os.listdir(local_resource_path)
                ]
                async with asyncssh.connect(
                    self.remote_host,
                    username=username,
                    client_keys=[(k, c)],
                    known_hosts=None,
                ) as conn:
                    await asyncssh.scp(files, (conn, self.resource_path))

        if self.ssh_forward_tunnel_client:
            # Establish an SSH remote forward tunnel
            with TemporaryDirectory() as td:
                local_resource_path = td
                _ = self.stage_ssh_keys(
                    self.ssh_forward_credentials_paths, local_resource_path
                )

                # create resource path dir in user's home on remote
                # async with asyncssh.connect(
                #     self.remote_host,
                #     username=username,
                #     client_keys=[(k, c)],
                #     known_hosts=None,
                # ) as conn:
                #     mkdir_cmd = "mkdir -p {path} 2>/dev/null".format(
                #         path=self.ssh_forward_tunnel_client_path
                #     )
                #     _ = await conn.run(mkdir_cmd)

                # # copy files
                # files = [
                #     os.path.join(local_resource_path, f)
                #     for f in os.listdir(local_resource_path)
                # ]
                # async with asyncssh.connect(
                #     self.remote_host,
                #     username=username,
                #     client_keys=[(k, c)],
                #     known_hosts=None,
                # ) as conn:
                #     await asyncssh.scp(files, (conn, self.ssh_forward_tunnel_client_path))

        if self.hub_api_url != "":
            old = "--hub-api-url={}".format(self.hub.api_url)
            new = "--hub-api-url={}".format(self.hub_api_url)
            for index, value in enumerate(cmd):
                if value == old:
                    cmd[index] = new

        for index, value in enumerate(cmd):
            if value[0:6] == "--port":
                cmd[index] = "--port=%d" % (port)



        remote_cmd = " ".join(cmd)

        # prepare the ssh_backtunnel
        forward_stated = await self.start_ssh_remote_forward_session()
        self.log.debug("Started SSH Remote Forward: {}".format(forward_stated))

        self.pid = await self.exec_notebook(remote_cmd)

        self.log.debug("Starting User: {}, PID: {}".format(self.user.name, self.pid))

        if self.pid < 0:
            return None

        return (self.remote_host, port)

    async def poll(self):
        """Poll ssh-spawned process to see if it is still running.

        If it is still running return None. If it is not running return exit
        code of the process if we have access to it, or 0 otherwise."""

        if not self.pid:
            # no pid, not running
            self.clear_state()
            return 0

        # send signal 0 to check if PID exists
        alive = await self.remote_signal(0)
        self.log.debug("Polling returned {}".format(alive))

        if not alive:
            self.clear_state()
            return 0
        else:
            return None

    async def stop(self, now=False):
        """Stop single-user server process for the current user."""
        _ = await self.remote_signal(15)
        self.clear_state()

    def get_remote_user(self, username):
        """Map JupyterHub username to remote username."""
        return username

    async def choose_remote_host(self):
        """
        Given the list of possible nodes from which to choose,
        make the choice of which should be the remote host.
        """
        remote_host = random.choice(self.remote_hosts)
        return remote_host

    @observe("remote_host")
    def _log_remote_host(self, change):
        self.log.debug("Remote host was set to %s." % self.remote_host)

    # FIXME this needs to now return IP and port too
    async def remote_random_port(self):
        """Select unoccupied port on the remote host and return it.

        If this fails for some reason return `None`."""

        username = self.get_remote_user(self.user.name)
        kf = self.ssh_keyfile.format(username=username)
        cf = kf + "-cert.pub"
        k = asyncssh.read_private_key(kf)
        c = asyncssh.read_certificate(cf)

        # this needs to be done against remote_host, first time we're calling up
        async with asyncssh.connect(
            self.remote_host, username=username, client_keys=[(k, c)], known_hosts=None
        ) as conn:
            result = await conn.run(self.remote_port_command)
            stdout = result.stdout
            stderr = result.stderr
            retcode = result.exit_status

        if stdout != b"":
            port = stdout
            port = int(port)
            self.log.debug("port={}".format(port))
        else:
            port = None
            self.log.error("Failed to get a remote port")
            self.log.error("STDERR={}".format(stderr))
            self.log.debug("EXITSTATUS={}".format(retcode))

        ip = self.remote_host
        return (ip, port)

    async def launch_detach_process(self, cmd):
        # Launches the cmd and detaches is from the python command
        result = subprocess.run([cmd],)
        return True

    async def start_ssh_remote_forward_session(self):
        env = super(CloudSSHSpawner, self).get_env()
        env["JUPYTERHUB_API_URL"] = self.hub_api_url
        env["JUPYTERHUB_ACTIVITY_URL"] = self.hub_activity_url
        env["JUPYTERHUB_HOST"] = self.hub_public_host
        env["PATH"] = self.path
        kf = self.ssh_keyfile.format(username=self.user.name)
        cf = kf + "-cert.pub"
        k = asyncssh.read_private_key(kf)
        c = asyncssh.read_certificate(cf)

        # ssh -v -fNT -R 8081:multiplespawner.workers.vcn.oraclevcn.com:8081 -R 8000:127.0.0.1:8000 ras6@130.61.232.102 -i ~/.corc/ssh/ras6_id_rsa
        self.log.debug("ssh remote forward target: {}".format(self.hub_public_host))
        # -R remote_port,local_host:local_port -R remote_port,local_host:local_port remote_user@remoteip -i path_to_rsa_key
        ssh_backtunnel_command = "ssh -fNT -R {}:{}:{} -R {}:{}:{} {}@{} -i {}".format(
            self.hub_api_port,
            self.hub_api_interface,
            self.hub_api_port,
            self.hub.port,
            self.hub.ip,
            self.hub.port,
            self.user.name,
            self.remote_host,
            self.ssh_forward_credentials_paths["private_key_file"],
        )

        username = self.get_remote_user(self.user.name)
        bash_script_str = "#!/bin/bash\n"
        bash_script_str += (
            "%s < /dev/null >> .{}_ssh_remote_forward.log 2>&1 & pid=$!\n".format(
                self.user.name
            )
            % ssh_backtunnel_command
        )
        run_script = "/tmp/{}_ssh_remote_forward_run.sh".format(self.user.name)
        with open(run_script, "w") as f:
            f.write(bash_script_str)
        if not os.path.isfile(run_script):
            raise Exception("The file " + run_script + "was not created.")
        else:
            with open(run_script, "r") as f:
                self.log.debug(run_script + " was written as:\n" + f.read())

        # Set the executable permission
        if not chmod(run_script, 0o755):
            raise Exception(
                "Failed to set executable permissions on: {}".format(run_script)
            )

        launched = await self.launch_detach_process(run_script)
        if not launched:
            raise Exception("Failed to execute: {}".format(run_script))
        return True

    # FIXME add docstring
    async def exec_notebook(self, command):
        """TBD"""

        env = super(CloudSSHSpawner, self).get_env()
        env["JUPYTERHUB_API_URL"] = self.hub_api_url
        env["JUPYTERHUB_ACTIVITY_URL"] = self.hub_activity_url
        env["PATH"] = self.path
        username = self.get_remote_user(self.user.name)
        kf = self.ssh_keyfile.format(username=username)
        cf = kf + "-cert.pub"
        k = asyncssh.read_private_key(kf)
        c = asyncssh.read_certificate(cf)
        bash_script_str = "#!/bin/bash\n"

        for item in env.items():
            # item is a (key, value) tuple
            # command = ('export %s=%s;' % item) + command
            bash_script_str += "export %s=%s\n" % item
        bash_script_str += "unset XDG_RUNTIME_DIR\n"

        bash_script_str += "touch .jupyter.log\n"
        bash_script_str += "chmod 600 .jupyter.log\n"
        bash_script_str += "%s < /dev/null >> .jupyter.log 2>&1 & pid=$!\n" % command
        bash_script_str += "echo $pid\n"

        run_script = "/tmp/{}_run.sh".format(self.user.name)
        with open(run_script, "w") as f:
            f.write(bash_script_str)
        if not os.path.isfile(run_script):
            raise Exception("The file " + run_script + "was not created.")
        else:
            with open(run_script, "r") as f:
                self.log.debug(run_script + " was written as:\n" + f.read())

        async with asyncssh.connect(
            self.remote_host, username=username, client_keys=[(k, c)], known_hosts=None
        ) as conn:
            result = await conn.run("bash -s", stdin=run_script)
            stdout = result.stdout
            _ = result.stderr
            retcode = result.exit_status

        self.log.debug("exec_notebook status={}".format(retcode))
        if stdout != b"":
            pid = int(stdout)
        else:
            return -1

        return pid

    async def remote_signal(self, sig):
        """Signal on the remote host."""

        username = self.get_remote_user(self.user.name)
        kf = self.ssh_keyfile.format(username=username)
        cf = kf + "-cert.pub"
        k = asyncssh.read_private_key(kf)
        c = asyncssh.read_certificate(cf)

        command = "kill -s %s %d < /dev/null" % (sig, self.pid)

        async with asyncssh.connect(
            self.remote_host, username=username, client_keys=[(k, c)], known_hosts=None
        ) as conn:
            result = await conn.run(command)
            stdout = result.stdout
            stderr = result.stderr
            retcode = result.exit_status
        self.log.debug(
            "command: {} returned {} --- {} --- {}".format(
                command, stdout, stderr, retcode
            )
        )
        return retcode == 0

    def stage_ssh_keys(self, paths, dest):
        # Expand paths if they are relative to the user
        paths["private_key_file"] = os.path.expanduser(paths["private_key_file"])
        paths["public_key_file"] = os.path.expanduser(paths["public_key_file"])

        shutil.copy(paths["private_key_file"], dest)
        shutil.copy(paths["public_key_file"], dest)

        private_key_file = os.path.basename(paths["private_key_file"])
        public_key_file = os.path.basename(paths["public_key_file"])

        private_key_path = os.path.join(
            self.ssh_forward_tunnel_client_path, private_key_file
        )
        public_key_path = os.path.join(
            self.ssh_forward_tunnel_client_path, public_key_file
        )

        return {
            "private_key_path": private_key_path,
            "public_key_path": public_key_path,
        }

    def stage_certs(self, paths, dest):
        # Expand paths if they are relative to the user
        paths["keyfile"] = os.path.expanduser(paths["keyfile"])
        paths["certfile"] = os.path.expanduser(paths["certfile"])
        paths["cafile"] = os.path.expanduser(paths["cafile"])

        shutil.move(paths["keyfile"], dest)
        shutil.move(paths["certfile"], dest)
        shutil.copy(paths["cafile"], dest)

        key_base_name = os.path.basename(paths["keyfile"])
        cert_base_name = os.path.basename(paths["certfile"])
        ca_base_name = os.path.basename(paths["cafile"])

        key = os.path.join(self.resource_path, key_base_name)
        cert = os.path.join(self.resource_path, cert_base_name)
        ca = os.path.join(self.resource_path, ca_base_name)

        return {
            "keyfile": key,
            "certfile": cert,
            "cafile": ca,
        }
