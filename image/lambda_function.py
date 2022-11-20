import boto3
import botocore
# import jsonschema
import json
import traceback
import zipfile
import os
import hashlib

from botocore.exceptions import ClientError

from extutil import remove_none_attributes, account_context, ExtensionHandler, ext, \
    current_epoch_time_usec_num, component_safe_name, lambda_env, random_id, \
    handle_common_errors, create_zip

eh = ExtensionHandler()

ecr = boto3.client('ecr')

def lambda_handler(event, context):
    try:
        print(f"event = {event}")
        account_number = account_context(context)['number']
        region = account_context(context)['region']
        eh.capture_event(event)

        prev_state = event.get("prev_state") or {}
        bucket = event.get("bucket")
        object_name = event.get("s3_object_name")
        # project_code = event.get("project_code")
        # repo_id = event.get("repo_id")
        # cname = event.get("component_name")

        cdef = event.get("component_def")

        repo_name = cdef.get("repo_name")
        if not repo_name:
            eh.add_log("repo_name is Required; Exiting", cdef, is_error=True)
            eh.perm_error("repo_name is Required", 0)

        docker_tags = cdef.get("docker_tags") or ["latest"]
        trust_level = cdef.get("trust_level") or "code"
        
        codebuild_project_override_def = cdef.get("Codebuild Project") or {} #For codebuild project overrides
        codebuild_build_override_def = cdef.get("Codebuild Build") or {} #For codebuild build overrides
    
        op = event.get("op")

        if event.get("pass_back_data"):
            print(f"pass_back_data found")
        elif op == "upsert":
            eh.add_op("load_initial_props")
            eh.add_op("setup_codebuild_project")
            if trust_level in ["full", "code"]: #At the moment these two are the same
                eh.add_op("compare_defs")

        elif op == "delete":
            eh.add_op("setup_codebuild_project")

        compare_defs(event)
        compare_etags(event, bucket, object_name)
        load_initial_props(bucket, object_name)
        setup_codebuild_project(bucket, object_name, codebuild_project_override_def, region, account_number, repo_name, docker_tags, op)
        run_codebuild_build(codebuild_build_override_def)
        get_final_props(repo_name, docker_tags)

        return eh.finish()

    except Exception as e:
        msg = traceback.format_exc()
        print(msg)
        eh.add_log("Unexpected Error", {"error": msg}, is_error=True)
        eh.declare_return(200, 0, error_code=str(e))
        return eh.finish()

def get_s3_etag(bucket, object_name):
    s3 = boto3.client("s3")

    try:
        s3_metadata = s3.head_object(Bucket=bucket, Key=object_name)
        print(f"s3_metadata = {s3_metadata}")
        eh.add_state({"zip_etag": s3_metadata['ETag']})
    except s3.exceptions.NoSuchKey:
        eh.add_log("Cound Not Find Zipfile", {"bucket": bucket, "key": object_name})
        eh.retry_error("Object Not Found")

@ext(handler=eh, op="compare_defs")
def compare_defs(event):
    old_digest = event.get("prev_state", {}).get("props", {}).get("def_hash")
    new_rendef = event.get("component_def")

    _ = new_rendef.pop("trust_level", None)

    dhash = hashlib.md5()
    dhash.update(json.dumps(new_rendef, sort_keys=True).encode())
    digest = dhash.hexdigest()
    eh.add_props({"def_hash": digest})

    if old_digest == digest:
        eh.add_log("Definitions Match, Checking Code", {"old_hash": old_digest, "new_hash": digest})
        eh.add_op("compare_etags") #Should hash definition

    else:
        eh.add_log("Definitions Don't Match, Deploying", {"old": old_digest, "new": digest})

@ext(handler=eh, op="compare_etags")
def compare_etags(event, bucket, object_name):
    old_props = event.get("prev_state", {}).get("props", {})

    initial_etag = old_props.get("initial_etag")

    #Get new etag
    get_s3_etag(bucket, object_name)
    if eh.state.get("zip_etag"):
        new_etag = eh.state["zip_etag"]
        if initial_etag == new_etag:
            eh.add_log("Elevated Trust: No Change Detected", {"initial_etag": initial_etag, "new_etag": new_etag})
            eh.add_props(old_props)
            eh.add_links(event.get("prev_state", {}).get("links", {}))
            eh.add_state(event.get("prev_state", {}).get("state", {}))
            eh.declare_return(200, 100, success=True)

        else:
            eh.add_op("setup_codebuild_project")
            eh.add_log("Code Changed, Deploying", {"old_etag": initial_etag, "new_etag": new_etag})

@ext(handler=eh, op="load_initial_props")
def load_initial_props(bucket, object_name):
    get_s3_etag(bucket, object_name)
    if eh.state.get("zip_etag"):
        eh.add_props({"initial_etag": eh.state.get("zip_etag")})

@ext(handler=eh, op="setup_codebuild_project")
def setup_codebuild_project(bucket, object_name, codebuild_def, region, account_number, repo_name, docker_tags, op):

    environment_variables = {
        "AWS_DEFAULT_REGION": region,
        "AWS_ACCOUNT_ID": account_number,
        "IMAGE_REPO_NAME": repo_name,
    }

    build_commands = [
        "echo Build started on `date`",
        "echo Building the Docker image...",
    ]

    post_build_commands = [
        "echo Build completed on `date`",
        "echo Pushing the Docker image...",
    ]

    actual_build_command = f"docker build "

    if not docker_tags:
        docker_tags = ["latest"]

    tag_build_commands = []
    for i, tag in enumerate(docker_tags):
        environment_variables[f"IMAGE_TAG_{i}"] = tag
        actual_build_command += f"-t $IMAGE_REPO_NAME:$IMAGE_TAG_{i} "
        tag_build_commands += [f"docker tag $IMAGE_REPO_NAME:$IMAGE_TAG_{i} $AWS_ACCOUNT_ID.dkr.ecr.$AWS_DEFAULT_REGION.amazonaws.com/$IMAGE_REPO_NAME:$IMAGE_TAG_{i}"] 
        post_build_commands += [f"docker push $AWS_ACCOUNT_ID.dkr.ecr.$AWS_DEFAULT_REGION.amazonaws.com/$IMAGE_REPO_NAME:$IMAGE_TAG_{i}"]

    actual_build_command += f"."
    build_commands += [actual_build_command] + tag_build_commands

    component_def = {
        "s3_bucket": bucket,
        "s3_object": object_name,
        "environment_variables": environment_variables,
        "pre_build_commands": [
            "echo Logging in to Amazon ECR...",
            "aws ecr get-login-password --region $AWS_DEFAULT_REGION | docker login --username AWS --password-stdin $AWS_ACCOUNT_ID.dkr.ecr.$AWS_DEFAULT_REGION.amazonaws.com"
        ],
        "build_commands": build_commands,
        "post_build_commands": post_build_commands,
        "container_image": "aws/codebuild/standard:6.0",
        "privileged_mode": True
    }

    #Allows for custom overrides as the user sees fit
    component_def.update(codebuild_def)

    eh.invoke_extension(
        arn=lambda_env("codebuild_project_lambda_name"), 
        component_def=component_def, 
        child_key="Codebuild Project", progress_start=25, 
        progress_end=30
    )

    if op == "upsert":
        eh.add_op("run_codebuild_build")

@ext(handler=eh, op="run_codebuild_build")
def run_codebuild_build(codebuild_build_def):
    print(eh.props)
    print(eh.links)

    component_def = {
        "project_name": eh.props["Codebuild Project"]["name"]
    }

    component_def.update(codebuild_build_def)

    eh.invoke_extension(
        arn=lambda_env("codebuild_build_lambda_name"),
        component_def=component_def, 
        child_key="Codebuild Build", progress_start=30, 
        progress_end=45
    )

    eh.add_op("get_final_props")


@ext(handler=eh, op="get_final_props")
def get_final_props(repo_name, tags):
    tag = tags[0]

    try:
        response = ecr.describe_images(
            repositoryName=repo_name,
            imageIds=[{"imageTag": tag}]
        )

        digest_value = response["imageDetails"][0]["imageDigest"]

        eh.add_props({
            "uri": f"{repo_name}:{digest_value}",
            "digest": digest_value,
            "tags": tags
        })

    except ClientError as e:
        handle_common_errors(e, eh, "Get Final Props", 90)

def format_tags(tags_dict):
    return [{"Key": k, "Value": v} for k,v in tags_dict]

def unformat_tags(tags_list):
    return {t["Key"]: t["Value"] for t in tags_list}

def gen_codebuild_arn(codebuild_project_name, region, account_number):
    return f"arn:aws:codebuild:{region}:{account_number}:project/{codebuild_project_name}"

def gen_codebuild_link(codebuild_project_name):
    return f"https://console.aws.amazon.com/codesuite/codebuild/projects/{codebuild_project_name}"



