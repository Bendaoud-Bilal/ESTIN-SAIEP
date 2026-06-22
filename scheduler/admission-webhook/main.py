from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import base64
import jsonpatch
import copy

app = FastAPI()

@app.post("/mutate")
async def mutate_pod(request: Request):
    """
    MutatingAdmissionWebhook:
    1. Reads requested vCPU/RAM from form annotations.
    2. Injects them as limits/requests into the Pod spec.
    3. Injects the Isolator SecurityContext and RuntimeClass.
    """
    req_data = await request.json()
    uid = req_data["request"]["uid"]
    pod = req_data["request"]["object"]
    
    modified_pod = copy.deepcopy(pod)
    annotations = modified_pod.get("metadata", {}).get("annotations", {})
    
    # 1. Read Sizing Form data (Fallback to defaults if not present)
    cpu_request = annotations.get("saiep.estin.dz/cpu", "1")
    mem_request = annotations.get("saiep.estin.dz/ram", "2Gi")
    
    # 2. Inject RuntimeClass for the Isolator tier (gVisor)
    modified_pod["spec"]["runtimeClassName"] = "gvisor-sandbox"
    
    # 3. Apply Resource Limits and SecurityContext to all containers
    for container in modified_pod["spec"].get("containers", []):
        # Resource Enforcement
        container["resources"] = {
            "requests": {"cpu": cpu_request, "memory": mem_request},
            "limits": {"cpu": cpu_request, "memory": mem_request}
        }
        
        # SecurityContext (Logical Sandbox Baseline)
        container["securityContext"] = {
            "runAsNonRoot": True,
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]}
        }
        
    # Generate JSON Patch
    patch = jsonpatch.JsonPatch.from_diff(pod, modified_pod)
    patch_b64 = base64.b64encode(str(patch).encode()).decode()
    
    return JSONResponse({
        "apiVersion": "admission.k8s.io/v1",
        "kind": "AdmissionReview",
        "response": {
            "uid": uid,
            "allowed": True,
            "patchType": "JSONPatch",
            "patch": patch_b64
        }
    })

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8443, ssl_keyfile="/certs/tls.key", ssl_certfile="/certs/tls.crt")