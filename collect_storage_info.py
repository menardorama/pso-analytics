import kubernetes
import purestorage
import purity_fb
import tabulate

import prometheus_client
import prometheus_client.core

import argparse
import base64
import copy
import json
import os
import re
import time
import urllib3

# Constants
PROMETHEUS_PORT=9492

# Helper to output byte values in human readable format.
def as_human_readable(input_bytes):
    if input_bytes < 1024:
        return str(input_bytes)
    elif input_bytes < (1024 ** 2):
        return str(round(input_bytes / 1024, 1)) + "K"
    elif input_bytes < (1024 ** 3):
        return str(round(input_bytes / (1024 ** 2), 1)) + "M"
    elif input_bytes < (1024 ** 4):
        return str(round(input_bytes / (1024 ** 3), 1)) + "G"
    elif input_bytes < (1024 ** 5):
        return str(round(input_bytes / (1024 ** 4), 1)) + "T"
    elif input_bytes < (1024 ** 6):
        return str(round(input_bytes / (1024 ** 5), 1)) + "P"
    else:
        return str(round(input_bytes / (1024 ** 6), 1)) + "E"


def sum_volume_records(x, y):
    return {k: x.get(k, 0) + y.get(k, 0) for k in set(x) | set(y)}

def prettify_record(r):
    drr = round(r["logical_bytes"] / r["physical_bytes"], 1) if r["physical_bytes"] > 0 else 1.0

    newr = {"drr": drr}
    newr.update(r)
    for l in ["logical_bytes", "physical_bytes", "provisioned_bytes"]:
        newr[l] = as_human_readable(newr[l])
    return newr


###############################################################################

parser = argparse.ArgumentParser()

parser.add_argument('--output', choices=['table', 'json'],
                    default='table',
                    help='Output format.')
parser.add_argument('--poll-seconds', default=3600, type=int, help='Frequency to poll stats')
parser.add_argument('--prometheus', action='store_true', help='Enable Prometheus endpoint')
args = parser.parse_args()


def collect_volumes():
    #========= Login to Kubernetes cluster ======================

    # Configs can be set in Configuration class directly or using helper utility
    kubernetes.config.load_incluster_config()

    v1 = kubernetes.client.CoreV1Api()

    # Collect state about each PVC found in the system.
    pvcs = {}
    ret = v1.list_persistent_volume_claim_for_all_namespaces(watch=False)
    for i in ret.items:
        pvcs[i.metadata.uid] = {"name": i.metadata.name, "namespace":
            i.metadata.namespace, "storageclass": i.spec.storage_class_name,
            "labels": i.metadata.labels}

    # To group PVCs by StatefulSet, create regexes that matches the naming
    # convention for the PVCs that belong to VolumeClaimTemplates.
    ss_regexes = {}
    ret = kubernetes.client.AppsV1Api().list_stateful_set_for_all_namespaces(watch=False)
    for i in ret.items:
        if i.spec.volume_claim_templates:
            for vct in i.spec.volume_claim_templates:
                ssname = i.metadata.name + "." + i.metadata.namespace
                ss_regexes[ssname] = re.compile(vct.metadata.name + "-" + i.metadata.name + "-[0-9]+")

    # Search for PURE_K8S_NAMESPACE
    pso_namespace = ""
    pso_prefix = ""
    ret = v1.list_pod_for_all_namespaces(watch=False)
    for i in ret.items:
        for c in i.spec.containers:
            if c.env:
                for e in c.env:
                    if e.name == "PURE_K8S_NAMESPACE":
                        pso_prefix = e.value
                        pso_namespace = i.metadata.namespace
                        break

    if not pso_namespace:
        print("Did not find PSO, exiting")
        exit(1)

    # Find the secret associated with the pure-provisioner in order to find the
    # login info for all FlashArrays and FlashBlades.
    flashblades={}
    flasharrays={}

    secrets = v1.read_namespaced_secret("pure-provisioner-secret", pso_namespace)
    rawbytes = base64.b64decode(secrets.data['pure.json'])
    purejson = json.loads(rawbytes.decode("utf-8"))
    pso_namespace = i.metadata.namespace
    flashblades = purejson["FlashBlades"] if "FlashBlades" in purejson else {}
    flasharrays = purejson["FlashArrays"] if "FlashArrays" in purejson else {}

    # Begin collecting and correlating volume information from the backends.
    vols = []

    # Disable warnings due to unsigned SSL certs.
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    #========= Login to FlashArrays ======================

    for fajson in flasharrays:
        fa = purestorage.FlashArray(fajson["MgmtEndPoint"], api_token=fajson["APIToken"])

        try:
            for vol in fa.list_volumes(names=[pso_prefix + "*"], space=True):
                assert vol["name"].startswith(pso_prefix + "-pvc-")
                uid = vol["name"].replace(pso_prefix + "-pvc-", "")

                if uid not in pvcs:
                    print("Found orphan PersistentVolume: " + uid + " on FlashArray " + fajson["MgmtEndPoint"])
                    continue

                pvc = pvcs[uid]

                tags = {"all": "all",
                        "storageclass": pvc["storageclass"],
                        "namespace": pvc["namespace"],
                        "name": pvc["name"],
                        "backend": "FA " + fajson["MgmtEndPoint"]}
                if pvc["labels"]:
                    for l in pvc["labels"]:
                        tags["label/" + l] = pvc["labels"][l]
                for ssname,rgx in ss_regexes.items():
                    if rgx.match(pvc["name"]):
                        tags["statefulset"] = ssname

                vol = {"uid": uid,
                       "logical_bytes": vol["total"],
                       "physical_bytes": vol["volumes"] * vol["data_reduction"],
                       "data_reduction": vol["data_reduction"],
                       "provisioned_bytes": vol["size"],
                       "tags": tags}

                vols.append(vol)

        except:
            pass


    #========= Login to FlashBlades ======================

    for fbjson in flashblades:
        fb = purity_fb.PurityFb(fbjson["MgmtEndPoint"],
                                api_token=fbjson["APIToken"])

        res = fb.file_systems.list_file_systems(filter="name='" + pso_prefix + "*'")
        for fs in res.items:
            assert fs.name.startswith(pso_prefix + "-pvc-")
            uid = fs.name.replace(pso_prefix + "-pvc-", "")

            if uid not in pvcs:
                print("Found orphan PersistentVolume: " + uid + " on FlashBlade " + fbjson["MgmtEndPoint"])
                continue

            pvc = pvcs[uid]

            tags = {"all": "all",
                    "storageclass": pvc["storageclass"],
                    "namespace": pvc["namespace"],
                    "name": pvc["name"],
                    "backend": "FB " + fbjson["MgmtEndPoint"]}
            if pvc["labels"]:
                for l in pvc["labels"]:
                    tags["label/" + l] = pvc["labels"][l]
            for ssname,rgx in ss_regexes.items():
                if rgx.match(pvc["name"]):
                    tags["statefulset"] = ssname

            vol = {"uid": uid,
                   "logical_bytes": fs.space.virtual,
                   "physical_bytes": fs.space.total_physical,
                   "provisioned_bytes": fs.provisioned,
                   "tags": tags}

            vols.append(vol)

    return vols


def prom_data_model(label):
    # Modify labels to conform to the Prometheus data model.
    return label.replace("-", "_").replace("/", "_")


class CustomCollector(object):
    def collect(self):
        # Collect pso-analytics information.
        vols = collect_volumes()
        # Extra labels from the set of tags.
        labellist = list(set([prom_data_model(y) for x in
            [list(v["tags"].keys()) for v in vols] for y in x if y != "all"]))

        g_used = prometheus_client.core.GaugeMetricFamily('pso_volume_used_bytes',
                'Logical byte usage by volumes', labels=['uid'] + labellist)
        g_phys = prometheus_client.core.GaugeMetricFamily('pso_volume_physical_bytes',
                'Physical byte usage by volumes', labels=['uid'] + labellist)
        g_prov = prometheus_client.core.GaugeMetricFamily('pso_volume_provisioned_bytes',
                'Bytes provisioned for volumes', labels=['uid'] + labellist)
        g_dre = prometheus_client.core.GaugeMetricFamily('pso_volume_datareduction_ratio',
                'Data reduction ratio for volumes', labels=['uid'] + labellist)

        for v in vols:
            labelvalues = [v['uid']] + [v['tags'][t] if t in v['tags'] else '<none>' for t in labellist]
            g_used.add_metric(labelvalues, v['logical_bytes'])
            g_phys.add_metric(labelvalues, int(v['physical_bytes']))
            g_prov.add_metric(labelvalues, v['provisioned_bytes'])
            g_dre.add_metric(labelvalues, v['data_reduction'])
        yield g_used
        yield g_phys
        yield g_prov
        yield g_dre


#========= Main Entry Point ======================

if args.prometheus:
    prometheus_client.REGISTRY.register(CustomCollector())
    prometheus_client.start_http_server(PROMETHEUS_PORT)

while True:
    vols = collect_volumes()

    if args.output == 'table':
        # Grab the unique list of keys in the tags across all volumes.
        tablenames = list(set([y for x in [list(v["tags"].keys()) for v in vols] for y in x]))

        for tab in tablenames:
            thistab = {}
            print("\n==== {} =====".format(tab))
            for v in vols:
                if tab in v["tags"]:
                    key = v["tags"][tab]
                    newrow = {"logical_bytes": v["logical_bytes"],
                              "physical_bytes": v["physical_bytes"],
                              "provisioned_bytes": v["provisioned_bytes"],
                              "data_reduction": v["data_reduction"],
                              "volume_count": 1}
                    thistab[key] = sum_volume_records(thistab[key], newrow) if key in thistab else copy.copy(newrow)

            # Flatten to a list and put the "name" back into to the dict (for tabulate)
            finaltab = [{"name": k, **prettify_record(thistab[k])} for k in thistab.keys()]
            print(tabulate.tabulate(finaltab, headers="keys"))

    elif args.output == 'json':
        for v in vols:
            print(v)

    print("", flush=True)

    time.sleep(args.poll_seconds)
