import os
import re
import time

import aexpect

from avocado.utils import process

from virttest import data_dir
from virttest import env_process
from virttest import error_context
from virttest import nfs
from virttest import utils_disk
from virttest import utils_misc
from virttest import utils_test
from virttest.remote import scp_to_remote
from virttest.utils_windows import virtio_win
from virttest.qemu_devices import qdevices

from provider.storage_benchmark import generate_instance


@error_context.context_aware
def run(test, params, env):
    """
    Test virtio-fs by sharing the data between host and guest.
    Steps:
        1. Create shared directories on the host.
        2. Run virtiofsd daemons on the host.
        3. Boot a guest on the host with virtiofs options.
        4. Log into guest then mount the virtiofs targets.
        5. Generate files or run stress on the mount points inside guest.

    :param test: QEMU test object.
    :param params: Dictionary with the test parameters.
    :param env: Dictionary with test environment.
    """
    def get_viofs_exe(session):
        """
        Get viofs.exe from virtio win iso,such as E:\viofs\2k19\amd64
        """
        test.log.info("Get virtiofs exe full path.")
        media_type = params["virtio_win_media_type"]
        try:
            get_drive_letter = getattr(virtio_win, "drive_letter_%s" % media_type)
            get_product_dirname = getattr(virtio_win,
                                          "product_dirname_%s" % media_type)
            get_arch_dirname = getattr(virtio_win, "arch_dirname_%s" % media_type)
        except AttributeError:
            test.error("Not supported virtio win media type '%s'", media_type)
        viowin_ltr = get_drive_letter(session)
        if not viowin_ltr:
            test.error("Could not find virtio-win drive in guest")
        guest_name = get_product_dirname(session)
        if not guest_name:
            test.error("Could not get product dirname of the vm")
        guest_arch = get_arch_dirname(session)
        if not guest_arch:
            test.error("Could not get architecture dirname of the vm")

        exe_middle_path = ("{name}\\{arch}" if media_type == "iso"
                           else "{arch}\\{name}").format(name=guest_name,
                                                         arch=guest_arch)
        exe_file_name = "virtiofs.exe"
        exe_find_cmd = 'dir /b /s %s\\%s | findstr "\\%s\\\\"'
        exe_find_cmd %= (viowin_ltr, exe_file_name, exe_middle_path)
        exe_path = session.cmd(exe_find_cmd).strip()
        test.log.info("Found exe file '%s'", exe_path)
        return exe_path

    def get_stdev(file):
        """
        Get file's st_dev value.
        """
        stdev = session.cmd_output(cmd_get_stdev % file).strip()
        test.log.info("%s device id is %s.", file, stdev)
        return stdev

    def check_socket_group():
        """
        check socket path's user group
        """
        cmd_get_sock = params["cmd_get_sock"]
        for device in vm.devices:
            if isinstance(device, qdevices.QVirtioFSDev):
                sock_path = device.get_param("sock_path")
                break
        sock_path_info = process.system_output(cmd_get_sock % sock_path)
        group_name = sock_path_info.decode(encoding="utf-8",
                                           errors="strict").strip().split()[3]
        if group_name != socket_group:
            test.fail("Socket-group name is not correct.\nIt should be %s,but"
                      " the output is %s" % (socket_group, group_name))

    def is_autoit_finished(session, process_name):
        """
        Check whether the target process is finished running
        """
        check_proc_cmd = check_proc_temp % process_name
        status, output = session.cmd_status_output(check_proc_cmd)
        if status:
            return False
        return "autoit3" not in output.lower()

    def viofs_svc_create(cmd):
        """
        Only for windows guest, to create a virtiofs service.

        :param cmd: cmd to create virtiofs service
        """
        test.log.info("Register virtiofs service in Windows guest.")
        exe_path = get_viofs_exe(session)
        sc_create_s, sc_create_o = session.cmd_status_output(cmd % exe_path)
        if sc_create_s != 0:
            test.fail("Failed to register virtiofs service, output is %s" % sc_create_o)

    def viofs_svc_stop_start(action, cmd):
        """
        Only for windows guest, to start/stop VirtioFsSvc.

        :param action: stop or start.
        :param cmd: cmd to start or stop virtiofs service
        """
        error_context.context("Try to %s VirtioFsSvc service." % action,
                              test.log.info)
        status, ouput = session.cmd_status_output(cmd)
        if status != 0:
            test.fail("Could not %s VirtioFsSvc service, "
                      "detail: '%s'" % (action, output))

    # data io config
    test_file = params.get('test_file')
    folder_test = params.get('folder_test')
    cmd_dd = params.get('cmd_dd')
    cmd_md5 = params.get('cmd_md5')
    cmd_new_folder = params.get('cmd_new_folder')
    cmd_copy_file = params.get('cmd_copy_file')
    cmd_rename_folder = params.get('cmd_rename_folder')
    cmd_check_folder = params.get('cmd_check_folder')
    cmd_del_folder = params.get('cmd_del_folder')

    # soft link config
    cmd_symblic_file = params.get('cmd_symblic_file')
    cmd_symblic_folder = params.get('cmd_symblic_folder')

    # pjdfs test config
    cmd_pjdfstest = params.get('cmd_pjdfstest')
    cmd_unpack = params.get('cmd_unpack')
    cmd_yum_deps = params.get('cmd_yum_deps')
    cmd_autoreconf = params.get('cmd_autoreconf')
    cmd_configure = params.get('cmd_configure')
    cmd_make = params.get('cmd_make')
    pjdfstest_pkg = params.get('pjdfstest_pkg')
    username = params.get('username')
    password = params.get('password')
    port = params.get('file_transfer_port')

    # fio config
    fio_options = params.get('fio_options')
    io_timeout = params.get_numeric('io_timeout')

    # iozone config
    iozone_options = params.get('iozone_options')

    # xfstest config
    cmd_xfstest = params.get('cmd_xfstest')
    fs_dest_fs2 = params.get('fs_dest_fs2')
    cmd_download_xfstest = params.get('cmd_download_xfstest')
    cmd_yum_install = params.get('cmd_yum_install')
    cmd_make_xfs = params.get('cmd_make_xfs')
    cmd_setenv = params.get('cmd_setenv')
    cmd_setenv_nfs = params.get('cmd_setenv_nfs', '')
    cmd_useradd = params.get('cmd_useradd')
    cmd_get_tmpfs = params.get('cmd_get_tmpfs')
    cmd_set_tmpfs = params.get('cmd_set_tmpfs')
    size_mem1 = params.get('size_mem1')

    # git init config
    git_init_cmd = params.get("git_init_cmd")
    install_git_cmd = params.get("install_git_cmd")
    check_proc_temp = params.get("check_proc_temp")
    git_check_cmd = params.get("git_check_cmd")
    autoit_name = params.get("autoit_name")

    # create dir by winapi config
    create_dir_winapi_cmd = params.get("create_dir_winapi_cmd")

    # nfs config
    setup_local_nfs = params.get('setup_local_nfs')

    setup_hugepages = params.get("setup_hugepages", "no") == "yes"
    socket_group_test = params.get("socket_group_test", "no") == "yes"
    socket_group = params.get("socket_group")

    # setup_filesystem_on_host
    setup_filesystem_on_host = params.get("setup_filesystem_on_host")

    # st_dev check config
    cmd_get_stdev = params.get("cmd_get_stdev")
    nfs_mount_dst_name = params.get("nfs_mount_dst_name")
    if cmd_xfstest and not setup_hugepages:
        # /dev/shm is the default memory-backend-file, the default value is the
        # half of the host memory. Increase it to guest memory size to avoid crash
        ori_tmpfs_size = process.run(cmd_get_tmpfs, shell=True).stdout_text.replace("\n", "")
        test.log.debug("original tmpfs size is %s", ori_tmpfs_size)
        params["post_command"] = cmd_set_tmpfs % ori_tmpfs_size
        params["pre_command"] = cmd_set_tmpfs % size_mem1

    if setup_local_nfs:
        for fs in params.objects("filesystems"):
            nfs_params = params.object_params(fs)

            params["export_dir"] = nfs_params.get("export_dir")
            params["nfs_mount_src"] = nfs_params.get("nfs_mount_src")
            params["nfs_mount_dir"] = nfs_params.get("fs_source_dir")
            if cmd_get_stdev:
                fs_source_dir = nfs_params.get("fs_source_dir")
                params["nfs_mount_dir"] = os.path.join(fs_source_dir, nfs_mount_dst_name)
            nfs_local = nfs.Nfs(params)
            nfs_local.setup()

    if setup_filesystem_on_host:
        # create partition on host
        dd_of_on_host = params.get("dd_of_on_host")
        cmd_dd_on_host = params.get("cmd_dd_on_host")
        process.system(cmd_dd_on_host % dd_of_on_host, timeout=300)

        cmd_losetup_query_on_host = params.get("cmd_losetup_query_on_host")
        loop_device = process.run(
                cmd_losetup_query_on_host, timeout=60).stdout.decode().strip()
        if not loop_device:
            test.fail("Can't find a valid loop device! ")
        # loop device setups on host
        cmd_losetup_on_host = params.get("cmd_losetup_on_host")
        process.system(cmd_losetup_on_host % dd_of_on_host, timeout=60)
        # make filesystem on host
        fs_on_host = params.get("fs_on_host")
        cmd_mkfs_on_host = params.get("cmd_mkfs_on_host")
        cmd_mkfs_on_host = cmd_mkfs_on_host % str(fs_on_host)
        cmd_mkfs_on_host = cmd_mkfs_on_host + loop_device
        process.system(cmd_mkfs_on_host, timeout=60)
        # mount on host
        fs_source = params.get('fs_source_dir')
        base_dir = params.get('fs_source_base_dir',
                              data_dir.get_data_dir())
        if not os.path.isabs(fs_source):
            fs_source = os.path.join(base_dir, fs_source)
        if not utils_misc.check_exists(fs_source):
            utils_misc.make_dirs(fs_source)
        if not utils_disk.mount(loop_device, fs_source):
            test.fail("Fail to mount on host! ")

    try:
        vm = None
        if (cmd_xfstest or setup_local_nfs
                or setup_hugepages or setup_filesystem_on_host):
            params["start_vm"] = "yes"
            env_process.preprocess(test, params, env)

        os_type = params.get("os_type")
        vm = env.get_vm(params.get("main_vm"))
        vm.verify_alive()
        session = vm.wait_for_login()
        host_addr = vm.get_address()

        if socket_group_test:
            check_socket_group()

        if os_type == "windows":
            cmd_timeout = params.get_numeric("cmd_timeout", 120)
            driver_name = params["driver_name"]
            install_path = params["install_path"]
            check_installed_cmd = params["check_installed_cmd"] % install_path

            # Check whether windows driver is running,and enable driver verifier
            session = utils_test.qemu.windrv_check_running_verifier(session,
                                                                    vm, test,
                                                                    driver_name)
            # install winfsp tool
            error_context.context("Install winfsp for windows guest.",
                                  test.log.info)
            installed = session.cmd_status(check_installed_cmd) == 0
            if installed:
                test.log.info("Winfsp tool is already installed.")
            else:
                install_cmd = utils_misc.set_winutils_letter(session,
                                                             params["install_cmd"])
                session.cmd(install_cmd, cmd_timeout)
                if not utils_misc.wait_for(lambda: not session.cmd_status(
                        check_installed_cmd), 60):
                    test.error("Winfsp tool is not installed.")

        for fs in params.objects("filesystems"):
            fs_params = params.object_params(fs)
            fs_target = fs_params.get("fs_target")
            fs_dest = fs_params.get("fs_dest")

            fs_source = fs_params.get("fs_source_dir")
            base_dir = fs_params.get("fs_source_base_dir",
                                     data_dir.get_data_dir())
            if not os.path.isabs(fs_source):
                fs_source = os.path.join(base_dir, fs_source)

            host_data = os.path.join(fs_source, test_file)

            if os_type == "linux":
                error_context.context("Create a destination directory %s "
                                      "inside guest." % fs_dest, test.log.info)
                utils_misc.make_dirs(fs_dest, session)
                if not cmd_xfstest:
                    error_context.context("Mount virtiofs target %s to %s inside"
                                          " guest." % (fs_target, fs_dest),
                                          test.log.info)
                    if not utils_disk.mount(fs_target, fs_dest, 'virtiofs', session=session):
                        test.fail('Mount virtiofs target failed.')

            else:
                error_context.context("Start virtiofs service in guest.", test.log.info)
                viofs_sc_create_cmd = params["viofs_sc_create_cmd"]
                viofs_sc_start_cmd = params["viofs_sc_start_cmd"]
                viofs_sc_stop_cmd = params["viofs_sc_stop_cmd"]
                viofs_sc_query_cmd = params["viofs_sc_query_cmd"]


                test.log.info("Check if virtiofs service is registered.")
                status, output = session.cmd_status_output(viofs_sc_query_cmd)
                if "not exist as an installed service" in output:
                    viofs_svc_create(viofs_sc_create_cmd)

                test.log.info("Check if virtiofs service is started.")
                status, output = session.cmd_status_output(viofs_sc_query_cmd)
                if "RUNNING" not in output:
                    test.log.info("Start virtiofs service.")
                    viofs_svc_stop_start("start", viofs_sc_start_cmd)
                else:
                    test.log.info("Virtiofs service is running.")

                # enable debug log.
                viofs_debug_enable_cmd = params.get("viofs_debug_enable_cmd")
                viofs_log_enable_cmd = params.get("viofs_log_enable_cmd")
                if viofs_debug_enable_cmd and viofs_log_enable_cmd:
                    error_context.context("Check if virtiofs debug log is enabled in guest.", test.log.info)
                    cmd = params.get("viofs_reg_query_cmd")
                    ret = session.cmd_output(cmd)
                    if "debugflags" not in ret.lower() or "debuglogfile" not in ret.lower():
                        error_context.context("Configure virtiofs debug log.", test.log.info)
                        for reg_cmd in (viofs_debug_enable_cmd, viofs_log_enable_cmd):
                            error_context.context("Set %s " % reg_cmd, test.log.info)
                            s, o = session.cmd_status_output(reg_cmd)
                            if s:
                                test.fail("Fail command: %s. Output: %s" % (reg_cmd, o))
                        error_context.context("Reboot guest.", test.log.info)
                        session = vm.reboot()
                    else:
                        test.log.info("Virtiofs debug log is enabled.")

                # get fs dest for vm
                virtio_fs_disk_label = fs_target
                error_context.context("Get Volume letter of virtio fs target, the disk"
                                      "lable is %s." % virtio_fs_disk_label,
                                      test.log.info)
                vol_con = "VolumeName='%s'" % virtio_fs_disk_label
                volume_letter = utils_misc.wait_for(
                    lambda: utils_misc.get_win_disk_vol(session, condition=vol_con), cmd_timeout)
                if volume_letter is None:
                    test.fail("Could not get virtio-fs mounted volume letter.")
                fs_dest = "%s:" % volume_letter

            guest_file = os.path.join(fs_dest, test_file)
            test.log.info("The guest file in shared dir is %s", guest_file)

            try:
                if cmd_dd:
                    error_context.context("Creating file under %s inside "
                                          "guest." % fs_dest, test.log.info)
                    session.cmd(cmd_dd % guest_file, io_timeout)

                    if os_type == "linux":
                        cmd_md5_vm = cmd_md5 % guest_file
                    else:
                        guest_file_win = guest_file.replace("/", "\\")
                        cmd_md5_vm = cmd_md5 % (volume_letter, guest_file_win)
                    md5_guest = session.cmd_output(cmd_md5_vm, io_timeout).strip().split()[0]

                    test.log.info(md5_guest)
                    md5_host = process.run("md5sum %s" % host_data,
                                           io_timeout).stdout_text.strip().split()[0]
                    if md5_guest != md5_host:
                        test.fail('The md5 value of host is not same to guest.')

                    viofs_log_file_cmd = params.get("viofs_log_file_cmd")
                    if viofs_log_file_cmd:
                        error_context.context("Check if LOG file is created.", test.log.info)
                        log_dir_s = session.cmd_status(viofs_log_file_cmd)
                        if log_dir_s != 0:
                            test.fail("Virtiofs log is not created.")

                if folder_test == 'yes':
                    error_context.context("Folder test under %s inside "
                                          "guest." % fs_dest, test.log.info)
                    session.cmd(cmd_new_folder % fs_dest)
                    try:
                        session.cmd(cmd_copy_file)
                        session.cmd(cmd_rename_folder)
                        session.cmd(cmd_del_folder)
                        status = session.cmd_status(cmd_check_folder)
                        if status == 0:
                            test.fail("The folder are not deleted.")
                    finally:
                        if os_type == "linux":
                            session.cmd("cd -")
                        else:
                            # there is no exit status for this cmd,so when getting
                            # the exit status, it actually get the status of last cmd.
                            # So use sendline function here.
                            session.sendline("C:")

                if cmd_symblic_file:
                    error_context.context("Symbolic test under %s inside "
                                          "guest." % fs_dest, test.log.info)
                    session.cmd(cmd_new_folder % fs_dest)
                    if session.cmd_status(cmd_symblic_file):
                        test.fail("Creat symbolic files failed.")
                    if session.cmd_status(cmd_symblic_folder):
                        test.fail("Creat symbolic folders failed.")
                    if os_type == "linux":
                        session.cmd("cd -")
                    else:
                        # there is no exit status for this cmd,so when getting
                        # the exit status, it actually get the status of last cmd.
                        # So use sendline function here.
                        session.sendline("C:")

                if fio_options:
                    error_context.context("Run fio on %s." % fs_dest, test.log.info)
                    fio = generate_instance(params, vm, 'fio')
                    try:
                        fio.run(fio_options % guest_file, io_timeout)
                    finally:
                        fio.clean()
                    vm.verify_dmesg()

                if iozone_options:
                    error_context.context("Run iozone test on %s." % fs_dest, test.log.info)
                    io_test = generate_instance(params, vm, 'iozone')
                    try:
                        io_test.run(iozone_options % guest_file, io_timeout)
                    finally:
                        io_test.clean()

                if cmd_pjdfstest:
                    error_context.context("Run pjdfstest on %s." % fs_dest, test.log.info)
                    host_path = os.path.join(data_dir.get_deps_dir('pjdfstest'), pjdfstest_pkg)
                    scp_to_remote(host_addr, port, username, password, host_path, fs_dest)
                    session.cmd(cmd_unpack.format(fs_dest), 180)
                    session.cmd(cmd_yum_deps, 180)
                    session.cmd(cmd_autoreconf % fs_dest, 180)
                    session.cmd(cmd_configure.format(fs_dest), 180)
                    session.cmd(cmd_make % fs_dest, io_timeout)
                    status, output = session.cmd_status_output(
                        cmd_pjdfstest % fs_dest, io_timeout)
                    if status != 0:
                        test.log.info(output)
                        test.fail('The pjdfstest failed.')

                if cmd_xfstest:
                    error_context.context("Run xfstest on guest.", test.log.info)
                    utils_misc.make_dirs(fs_dest_fs2, session)
                    if session.cmd_status(cmd_download_xfstest, 360):
                        test.error("Failed to download xfstests-dev")
                    session.cmd(cmd_yum_install, 180)

                    # Due to the increase of xfstests-dev cases, more time is
                    # needed for compilation here.
                    status, output = session.cmd_status_output(cmd_make_xfs, 900)
                    if status != 0:
                        test.log.info(output)
                        test.error("Failed to build xfstests-dev")
                    session.cmd(cmd_setenv, 180)
                    session.cmd(cmd_setenv_nfs, 180)
                    session.cmd(cmd_useradd, 180)

                    try:
                        output = session.cmd_output(cmd_xfstest, io_timeout)
                        test.log.info("%s", output)
                        if 'Failed' in output:
                            test.fail('The xfstest failed.')
                        else:
                            break
                    except (aexpect.ShellStatusError, aexpect.ShellTimeoutError):
                        test.fail('The xfstest failed.')

                if cmd_get_stdev:
                    error_context.context("Create files in local device and"
                                          " nfs device ", test.log.info)
                    file_in_local_host = os.path.join(fs_source, "file_test")
                    file_in_nfs_host = os.path.join(fs_source, nfs_mount_dst_name,
                                                    "file_test")
                    cmd_touch_file = "touch %s && touch %s" % (file_in_local_host,
                                                               file_in_nfs_host)
                    process.run(cmd_touch_file)
                    error_context.context("Check if the two files' st_dev are"
                                          " the same on guest.", test.log.info)
                    file_in_local_guest = os.path.join(fs_dest, "file_test")
                    file_in_nfs_guest = os.path.join(fs_dest, nfs_mount_dst_name,
                                                     "file_test")
                    if get_stdev(file_in_local_guest) == get_stdev(file_in_nfs_guest):
                        test.fail("st_dev are the same on diffrent device.")

                if git_init_cmd:
                    if os_type == "windows":
                        error_context.context("Install git", test.log.info)
                        check_status, check_output = session.cmd_status_output(git_check_cmd)
                        if check_status and "not recognized" in check_output:
                            install_git_cmd = utils_misc.set_winutils_letter(
                                session, install_git_cmd)
                            status, output = session.cmd_status_output(install_git_cmd)

                            if status:
                                test.error("Failed to install git, status=%s, output=%s"
                                           % (status, output))
                            test.log.info("Wait for git installation to complete")
                            utils_misc.wait_for(
                                lambda: is_autoit_finished(session, autoit_name), 360, 60, 5)
                    error_context.context("Git init test in %s" % fs_dest, test.log.info)
                    status, output = session.cmd_status_output(git_init_cmd % fs_dest)
                    if status:
                        test.fail("Git init failed with %s" % output)

                if create_dir_winapi_cmd:
                    error_context.context("Create new directory with WinAPI's "
                                          "CreateDirectory.", test.log.info)
                    s, o = session.cmd_status_output(create_dir_winapi_cmd)
                    if s:
                        test.fail("Create dir failed, output is %s", o)

                    error_context.context("Get virtiofsd log file.", test.log.info)
                    vfsd_dev = vm.devices.get_by_params({"source": fs_source})[0]
                    vfd_log_name = '%s-%s.log' % (vfsd_dev.get_qid(),
                                                  vfsd_dev.get_param('name'))
                    vfd_logfile = utils_misc.get_log_filename(vfd_log_name)

                    error_context.context("Check virtiofsd log.", test.log.info)
                    pattern = r'Replying ERROR.*header.*OutHeader.*error.*-9'
                    with open(vfd_logfile, 'r') as f:
                        for line in f.readlines():
                            if re.match(pattern, line, re.I):
                                test.fail("CreateDirectory cause virtiofsd-rs ERROR reply.")

                if params.get("stop_start_repeats") and os_type == "windows":
                    repeats = int(params.get("stop_start_repeats", 1))
                    for i in range(repeats):
                        error_context.context("Repeat stop/start VirtioFsSvc:"
                                              " %s/%s" % (i + 1, repeats),
                                              test.log.info)
                        viofs_svc_stop_start("stop", viofs_sc_stop_cmd)
                        time.sleep(1)
                        viofs_svc_stop_start("start", viofs_sc_start_cmd)
                        time.sleep(1)
                    error_context.context("Basic IO test after"
                                          " repeat stop/start virtiofs"
                                          " service.", test.log.info)
                    s, o = session.cmd_status_output(cmd_dd % guest_file, io_timeout)
                    if s:
                        test.fail("IO test failed, the output is %s" % o)

            finally:
                if os_type == "linux":
                    utils_disk.umount(fs_target, fs_dest, 'virtiofs', session=session)
                    utils_misc.safe_rmdir(fs_dest, session=session)
    finally:
        if setup_local_nfs:
            if vm and vm.is_alive():
                vm.destroy()
            for fs in params.objects("filesystems"):
                nfs_params = params.object_params(fs)
                params["export_dir"] = nfs_params.get("export_dir")
                params["nfs_mount_dir"] = nfs_params.get("fs_source_dir")
                params["rm_export_dir"] = nfs_params.get("export_dir")
                params["rm_mount_dir"] = nfs_params.get("fs_source_dir")
                if cmd_get_stdev:
                    fs_source_dir = nfs_params.get("fs_source_dir")
                    params["nfs_mount_dir"] = os.path.join(fs_source_dir, nfs_mount_dst_name)
                nfs_local = nfs.Nfs(params)
                nfs_local.cleanup()
                utils_misc.safe_rmdir(params["export_dir"])
        if setup_filesystem_on_host:
            cmd = "if losetup -l {0};then losetup -d {0};fi;".format(
                loop_device)
            cmd += "umount -l {0};".format(fs_source)
            process.system_output(cmd, shell=True, timeout=60)
            if utils_misc.check_exists(dd_of_on_host):
                cmd_del = "rm -rf " + dd_of_on_host
                process.run(cmd_del, timeout=60)

    # during all virtio fs is mounted, reboot vm
    if params.get('reboot_guest', 'no') == 'yes':
        def get_vfsd_num():
            """
            Get virtiofsd daemon number during vm boot up.
            :return: virtiofsd daemon count.
            """
            cmd_ps_virtiofsd = params.get('cmd_ps_virtiofsd')
            vfsd_num = 0
            for device in vm.devices:
                if isinstance(device, qdevices.QVirtioFSDev):
                    sock_path = device.get_param('sock_path')
                    cmd_ps_virtiofsd = cmd_ps_virtiofsd % sock_path
                    vfsd_ps = process.system_output(cmd_ps_virtiofsd, shell=True)
                    vfsd_num += len(vfsd_ps.strip().splitlines())
            return vfsd_num

        error_context.context("Check virtiofs daemon before reboot vm.",
                              test.log.info)

        vfsd_num_bf = get_vfsd_num()
        error_context.context("Reboot guest and check virtiofs daemon.",
                              test.log.info)
        vm.reboot()
        if not vm.is_alive():
            test.fail("After rebooting vm quit unexpectedly.")
        vfsd_num_af = get_vfsd_num()

        if vfsd_num_bf != vfsd_num_af:
            test.fail("Virtiofs daemon is different before and after reboot.\n"
                      "Before reboot: %s\n"
                      "After reboot: %s\n", (vfsd_num_bf, vfsd_num_af))
