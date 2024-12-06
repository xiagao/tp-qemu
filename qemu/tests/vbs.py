from virttest import env_process
from virttest import data_dir
import time


def run(test, params, env):
    """
    Please make sure the guest installed with signed driver
    Verify Secure MOR control feature using Device Guard tool in Windows guest:

    1) Boot up a guest.
    2) Check if Secure Boot is enable.
    3) Download Device Guard and copy to guest.
    4) Enable Device Guard and check the output.
    5) Reboot guest.
    6) Run Device Guard and check the output.
    7) Disable Device Guard and shutdown guest.

    :param test: QEMU test object
    :param params: Dictionary with the test parameters
    :param env: Dictionary with test environment.
    """

    def execute_powershell_command(command, timeout=60):
        status, output = session.cmd_status_output(command, timeout)
        if status != 0:
            test.fail("execute command fail: %s" % output)
        return output

    login_timeout = int(params.get("login_timeout", 360))
    params["ovmf_vars_filename"] = 'OVMF_VARS.secboot.fd'
    params["start_vm"] = 'yes'
    env_process.preprocess_vm(test, params, env, params['main_vm'])
    vm = env.get_vm(params["main_vm"])
    session = vm.wait_for_serial_login(timeout=login_timeout)

    check_cmd = params['check_secure_boot_enabled_cmd']
    dgreadiness_path_command = params['dgreadiness_path_command']
    executionPolicy_command = params['executionPolicy_command']
    enable_command = params['enable_command']
    disable_command = params['disable_command']
    ready_command = params['ready_command']
    dg_command = params['dg_command']
    check_ready_info = params['check_ready_info']

    try:
        output = session.cmd_output(check_cmd)
        if 'False' in output:
            test.fail('Secure boot is not enabled. The actual output is %s'
                      % output)

        # Copy Device Guard to guest
        dgreadiness_host_path = data_dir.get_deps_dir("dgreadiness")
        dst_path = params["dst_path"]
        test.log.info("Copy Device Guuard to guest.")
        s, o = session.cmd_status_output("mkdir %s" % dst_path)
        if s and "already exists" not in o:
            test.error("Could not create Device Guard directory in "
                       "VM '%s', detail: '%s'" % (vm.name, o))
        vm.copy_files_to(dgreadiness_host_path, dst_path)

        execute_powershell_command(dgreadiness_path_command)
        test.log.info("Before doing anything, check VBS status...")
        output = execute_powershell_command(ready_command)
        test.log.info("DG ready output: %s" % output)
        #output = execute_powershell_command(dg_command)
        #test.log.info("DG get output: %s" % output)
        if check_ready_info in output:
            test.log.info("VBS is already enabled, and guest boot up successfully")
            return

        test.log.info("Enable VBS...")
        time.sleep(2)
        execute_powershell_command(executionPolicy_command)
        output_enable = execute_powershell_command(enable_command)
        test.log.info("DG enable output: %s" % output_enable)
        time.sleep(2)
        #output = execute_powershell_command(ready_command)
        #test.log.info("DG ready output: %s" % output)
        time.sleep(2)
        output = execute_powershell_command(dg_command)
        test.log.info("DG get output: %s" % output)
        check_enable_info = params['check_enable_info']
        if check_enable_info not in output_enable:
            test.fail("Device Guard enable failed. The actual output is %s"
                      % output_enable)

        # Reboot guest and run Device Guard
        session = vm.reboot(session)
        execute_powershell_command(dgreadiness_path_command)
        execute_powershell_command(executionPolicy_command)
        output = execute_powershell_command(ready_command)
        test.log.info("DG ready output: %s" % output)
        o = execute_powershell_command(dg_command)
        test.log.info("DG get output: %s" % o)
        if check_ready_info not in output:
            test.fail("Device Guard running failed. The actual output is %s"
                      % output)

        test.log.info("Disable vbs...")
        output = execute_powershell_command(disable_command)
        test.log.info("DG disable output: %s" % output)
        #output = execute_powershell_command(ready_command)
        #test.log.info("DG ready output: %s" % output)
        output = execute_powershell_command(dg_command)
        test.log.info("DG get output: %s" % output)

    finally:
        session.close()
