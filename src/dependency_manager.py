import asyncio
import os
import socket
import kopf
import pykube
import kubernetes
import yaml


# define Colors for logger
class Color:
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'


@kopf.on.login(errors=kopf.ErrorsMode.PERMANENT)
async def login_fn(**_):
    print('Logging in in 2s...')
    await asyncio.sleep(2.0)

    config = pykube.KubeConfig.from_env()
    ca = config.cluster.get('certificate-authority')
    cert = config.user.get('client-certificate')
    pkey = config.user.get('client-key')
    return kopf.ConnectionInfo(
        server=config.cluster.get('server'),
        ca_path=ca.filename() if ca else None,  # can be a temporary file
        insecure=config.cluster.get('insecure-skip-tls-verify'),
        username=config.user.get('username'),
        password=config.user.get('password'),
        scheme='Bearer',
        token=config.user.get('token'),
        certificate_path=cert.filename() if cert else None,  # can be a temporary file
        private_key_path=pkey.filename() if pkey else None,  # can be a temporary file
        default_namespace=config.namespace,
    )


# webhook server config
@kopf.on.startup()
def config(settings: kopf.OperatorSettings, **_):
    settings.admission.managed = 'example.dependency-injector'
    hostname = socket.gethostname()
    operator_address = socket.gethostbyname(hostname)
    settings.admission.server = kopf.WebhookServer(host=operator_address, addr='0.0.0.0', port=8080)


@kopf.on.startup()
async def configure(settings: kopf.OperatorSettings, **_):
    # settings.network.retries = 3
    settings.networking.errors_backoff = 30
    settings.networking.request_timeout = 10
    settings.networking.connect_timeout = 10


@kopf.on.cleanup()
async def cleanup_fn(logger, **_):
    logger.info("Cleaning up...")
    # add some cleanup


# get the init_contianer definition based on dependencies found
def get_init_container(logger, dependencies):

    init_path = os.path.join(os.path.dirname(__file__), '../resources', 'init-example-dependency.yaml')
    tmpl = open(init_path, 'rt').read()
    dependencies = ",".join(dependencies)
    init_data = tmpl.format(http_code="http_code", depends_on=dependencies)
    init_yaml = yaml.safe_load(init_data)
    logger.info(f"{Color.OKCYAN}Adding following dependencies...{Color.ENDC}")
    logger.info(dependencies)
    return init_yaml


# get dependencies for pod in context
def get_dependency_objects(logger, app_name, namespace):
    api = kubernetes.client.CustomObjectsApi()
    dependency_objects = api.list_namespaced_custom_object(
    group="example.dependency-injector",
    version="v1",
    namespace=namespace,
    plural="dependencies")
    items_list = dependency_objects["items"]

    for custom_object in items_list:
        app = custom_object["spec"]["selector"]["app"]
        if app == app_name:
            logger.info(f"{Color.OKCYAN}Found Dependency object matching \
            the pod selector{Color.ENDC}")
            return custom_object["spec"]["depends_on"]

    logger.info(f"{Color.OKCYAN}no dependencies found for this application{Color.ENDC}")
    return None


def get_api(api_name, **_):
    api_list = api_name.split("/")

    # for i in range(len(api_list)):
    for index, item in enumerate(api_list):
        api_list[index] = item.capitalize()

    return ''.join(api_list) + "Api"


def get_pod_namespace(meta, logger, app_name):
    owner_uid = meta["ownerReferences"][0]["uid"]

    logger.debug(f"{Color.OKCYAN}pod ownerReferences :: {meta['ownerReferences']}{Color.ENDC}")

    label_selector = "app=" + app_name

    owner_kind = meta["ownerReferences"][0]["kind"]
    owner_api = meta["ownerReferences"][0]["apiVersion"]
    owner_api_version = get_api(api_name=owner_api, logger=logger)
    logger.debug(f"{Color.OKCYAN}extracted {owner_api_version} as API form {owner_api}{Color.ENDC}")

    if "Set" in owner_kind:
        kind = owner_kind.rstrip("Set").lower() + "_set"
    else:
        kind = owner_kind.lower()

    logger.debug(f"{Color.OKCYAN} Kind = {kind}{Color.ENDC}")

    api_func = getattr(kubernetes.client, owner_api_version)

    api = api_func()

    kind_func = "list_" + kind + "_for_all_namespaces"
    list_owner_for_all_namespaces = getattr(api, kind_func)

    obj = list_owner_for_all_namespaces(label_selector=label_selector)

    for pod in obj.items:
        name = pod.metadata.name
        uid = pod.metadata.uid
        logger.debug(f"{Color.OKCYAN}checking pod {name} for owner_references :: {uid}{Color.ENDC}")
        if uid == owner_uid:
            logger.debug(f"found uid ({owner_uid}) match for owner reference {uid}")
            return pod.metadata.namespace

    raise kopf.AdmissionError(f"{Color.FAIL}Cannot find namespace \
        for the pod{Color.ENDC}", code=499)


# mutate pod - patch to add init_container
def _mutate_pod(spec, namespace, logger, patch, body, meta):
    pod_label = body.get("metadata")["labels"]
    if "app" not in pod_label.keys():
        logger.info(f"{Color.OKBLUE}expected key 'app' not found in pod labels")
        return None

    app_name = pod_label["app"]

    if namespace is None:
        logger.debug(f"{Color.OKCYAN}no namespace found.\
        Executing get_pod_namespace Func{Color.ENDC}")
        namespace = get_pod_namespace(meta=meta, app_name=app_name, logger=logger)

    logger.info(f"{Color.OKBLUE}Processing for application\
     {app_name} in {namespace} namespace...{Color.ENDC}")
    dependencies_list = get_dependency_objects(logger=logger,
                                               app_name=app_name,
                                               namespace=namespace)

    if dependencies_list is None:
        logger.info(f"{Color.OKGREEN} Exiting call {Color.ENDC}")
        return None

    managed_init_container_list = get_init_container(logger=logger,
                                                     dependencies=dependencies_list)

    if "initContainers" in spec:
        patched_container_list = spec["initContainers"]
    else:
        patched_container_list = []

    patched_container_list.append(managed_init_container_list[0])

    try:
        logger.info(f"{Color.OKBLUE}executing patch now...{Color.ENDC}")
        patch.spec["initContainers"] = patched_container_list
    except Exception as exception:
        logger.info(f"{Color.FAIL}Mutate Func errored out with following exception{Color.ENDC}")
        logger.info(exception)


@kopf.on.mutate('pods',
                labels={'sidecar.dependency_manager': 'true'},
                operation="UPDATE",
                ignore_failures=True)
async def pod_update_handler(spec, namespace, logger, patch, body, meta, **_):
    logger.info(f"{Color.OKBLUE}Mutate webhook call initiated for pod update ...{Color.ENDC}")
    _mutate_pod(spec, namespace, logger, patch, body, meta)
    logger.info(f"{Color.OKGREEN}Finished patching pod {Color.ENDC}")

@kopf.on.mutate('pods',
                labels={'sidecar.dependency_manager': 'true'},
                operation="CREATE",
                ignore_failures=True)
async def pod_handler(spec, namespace, logger, patch, body, meta, **_):
    logger.info(f"{Color.OKBLUE}Mutate webhook call initiated for pod CREATE...{Color.ENDC}")
    _mutate_pod(spec, namespace, logger, patch, body, meta)
    logger.info(f"{Color.OKGREEN}Finished patching pod {Color.ENDC}")


def _validate_dependency_object(spec, name, logger):
    if spec is None:
        raise kopf.AdmissionError(f"{Color.FAIL}Cannot find \
            spec in definition{Color.ENDC}", code=499)

    if "depends_on" not in spec.keys():
        raise kopf.AdmissionError(f"{Color.FAIL}attribute 'depends_on' \
            not found in spec{Color.ENDC}", code=499)

    if "selector" not in spec.keys():
        raise kopf.AdmissionError(f"{Color.FAIL}attribute 'selector' \
            not found in spec{Color.ENDC}", code=499)

    depends_on = spec.get("depends_on")
    if not isinstance(depends_on, list):
        raise kopf.AdmissionError(f"{Color.FAIL}attribute 'depends_on' \
            should be of type list. Encountered {type(depends_on)} {Color.ENDC}", code=499)

    selector = spec.get("selector")
    if not isinstance(selector, dict):
        raise kopf.AdmissionError(f"{Color.FAIL}attribute 'selector' \
            should be of type dict. Encountered {type(selector)} {Color.ENDC}", code=499)

    if "app" not in selector.keys():
        raise kopf.AdmissionError(f"{Color.FAIL}attribute 'app' not \
            found in spec.selector{Color.ENDC}", code=499)

    app = selector["app"]
    if not isinstance(app, str):
        raise kopf.AdmissionError(f"{Color.FAIL}attribute 'app' should \
            be of type string. Encountered {type(app)} {Color.ENDC}", code=499)

    logger.info(f"{Color.OKGREEN}Admitted Dependency {name}{Color.ENDC}")
    return "Done"


@kopf.on.validate('dependency', ignore_failures=False, operation='CREATE')
def validate_dependency_on_create(spec, name, namespace, logger, patch, body, **_):
    logger.info(f"{Color.OKBLUE}Validate call initiated for Dependency {name} on CREATE{Color.ENDC}")
    _validate_dependency_object(spec, name, logger)
    logger.info(f"{Color.OKGREEN}Admitted Dependency {name} in {namespace}{Color.ENDC}")


@kopf.on.validate('dependency', ignore_failures=False, operation='UPDATE')
def validate_dependency_on_update(spec, name, namespace, logger, patch, body, **_):
    logger.info(f"{Color.OKBLUE}Validate call initiated for Dependency {name} on UPDATE {Color.ENDC}")
    _validate_dependency_object(spec, name, logger)
    logger.info(f"{Color.OKGREEN}Admitted Dependency {name} in {namespace}{Color.ENDC}")
