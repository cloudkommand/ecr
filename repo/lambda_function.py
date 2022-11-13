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
        project_code = event.get("project_code")
        repo_id = event.get("repo_id")
        cdef = event.get("component_def")
        cname = event.get("component_name")

        name = cdef.get("name") or component_safe_name(
            project_code, repo_id, cname, max_chars=255
        )

        registry_account_id = cdef.get("registry_account_id") or None
        tags = cdef.get("tags") or {}
        trust_level = cdef.get("trust_level")
        
        changeable_tags = cdef.get("changeable_tags") or "MUTABLE"
        if changeable_tags not in ["MUTABLE", "IMMUTABLE"]:
            raise Exception(f"Invalid changeable_tags value: {changeable_tags}")

        scan_on_push = cdef.get("scan_on_push") or False
        if scan_on_push not in [True, False]:
            raise Exception(f"Invalid scan_on_push value: {scan_on_push}")

        kms_key = cdef.get("kms_key_arn")
    
        if event.get("pass_back_data"):
            print(f"pass_back_data found")
        elif event.get("op") == "upsert":
            if trust_level == "full":
                eh.add_op("compare_defs")
            else:
                eh.add_op("get_repository")

        elif event.get("op") == "delete":
            eh.add_op("delete_repository", {"create_and_remove": False, "name": name})
            
        compare_defs(event)

        repo_def = remove_none_attributes({
            "registryId": registry_account_id,
            "repositoryName": name,
            "tags": format_tags(tags) or None,
            "imageTagMutability": changeable_tags, #Editable
            "imageScanningConfiguration": {
                "scanOnPush": scan_on_push
            },
            "encryptionConfiguration": {
                "encryptionType": "KMS",
                "kmsKey": kms_key
            } if kms_key else None
        })

        compare_defs(event)
        get_repository(name, repo_def, prev_state, region, account_number, tags)
        create_repository(name, repo_def)
        update_image_scanning_configuration(name, scan_on_push)
        update_image_tag_mutability(name, changeable_tags)
        add_tags()
        remove_tags()
        delete_repository()
            
        return eh.finish()

    except Exception as e:
        msg = traceback.format_exc()
        print(msg)
        eh.add_log("Unexpected Error", {"error": msg}, is_error=True)
        eh.declare_return(200, 0, error_code=str(e))
        return eh.finish()

@ext(handler=eh, op="compare_defs")
def compare_defs(event):
    old_rendef = event.get("prev_state", {}).get("rendef", {})
    new_rendef = event.get("component_def")

    _ = old_rendef.pop("trust_level", None)
    _ = new_rendef.pop("trust_level", None)

    if old_rendef != new_rendef:
        eh.add_op("get_repository")

    else:
        eh.add_links(event.get("prev_state", {}).get('links'))
        eh.add_props(event.get("prev_state", {}).get('props'))
        eh.add_log("Full Trust, No Change: Exiting", {"old": old_rendef, "new": new_rendef})

@ext(handler=eh, op="get_repository")
def get_repository(name, repo_def, prev_state, region, account_number, tags):

    if prev_state and prev_state.get("props") and prev_state.get("props").get("name"):
        prev_name = prev_state.get("props").get("name")
        if name != prev_name:
            eh.add_op("delete_repository", {
                "create_and_remove": True, 
                "name": prev_name,
                "registry_id": prev_state.get("props").get("registry_id")
            })


    try:
        params = remove_none_attributes({
            "repositoryNames": [name],
            "registryId": repo_def.get("registryId")
        })
        response = ecr.describe_repositories(**params)
        print(f"Get repo response: {response}")
        if response.get("repositories"):
            eh.add_log("Found Repository Project", response.get("repositories")[0])
            repo = response.get("repositories")[0]
            eh.add_props({
                "arn": repo['repositoryArn'],
                "name": repo['repositoryName'],
                "uri": repo['repositoryUri'],
                "registry_id": repo['registryId']
            })

            if (repo.get("encryptionConfiguration", {}).get("encryptionType") == "KMS") and not repo_def.get("encryptionConfiguration"):
                eh.add_log("WARNING: Create a New Component to Change Encryption Type", {"encryptionType": repo.get("encryptionConfiguration", {}).get("encryptionType")}, is_error=True)
            if repo.get("imageTagMutability") != repo_def.get("imageTagMutability"):
                eh.add_op("update_image_tag_mutability")
            if repo.get("imageScanningConfiguration", {}).get("scanOnPush") != repo_def.get("imageScanningConfiguration", {}).get("scanOnPush"):
                eh.add_op("update_scan_on_push")
            
            response = ecr.list_tags_for_resource(
                resourceArn=repo['repositoryArn']
            )
            print(f"tags response = {response}")
            current_tags = unformat_tags(response.get("Tags") or [])

            if tags != current_tags:
                remove_tags = [k for k in current_tags.keys() if k not in tags]
                add_tags = {k:v for k,v in tags.items() if k not in current_tags.keys()}
                if remove_tags:
                    eh.add_op("remove_tags", remove_tags)
                if add_tags:
                    eh.add_op("add_tags", add_tags)
        
        else:
            eh.add_op("create_repository")

    except botocore.exceptions.ClientError as e:
        if e.response['Error']['Code'] == 'RepositoryNotFoundException':
            eh.add_op("create_repository")
        else:
            handle_common_errors(e, eh, "Get Repository Failed", 10)

@ext(handler=eh, op="create_repository")
def create_repository(name, repo_def):

    try:
        response = ecr.create_repository(**repo_def).get("repository")
        eh.add_log("Created ECR Repository", response)
        eh.add_props({
            "arn": response['repositoryArn'],
            "name": response['repositoryName'],
            "uri": response['repositoryUri'],
            "registry_id": response['registryId']
        })
        # eh.add_links({"Codebuild Project": gen_codebuild_link(name)})
    except ClientError as e:
        handle_common_errors(
            e, eh, "Create ECR Repository Failed", 20,
            perm_errors=[
                "LimitExceededException", 
                "RepositoryAlreadyExistsException",
                "InvalidParameterException",
                "InvalidTagParameterException",
                "TooManyTagsException"
            ]
        )

@ext(handler=eh, op="update_image_scanning_configuration")
def update_image_scanning_configuration(name, scan_on_push):
    registry_id = eh.props.get("registry_id")

    try:
        response = ecr.put_image_scanning_configuration(
            registryId=registry_id,
            repositoryName=name,
            imageScanningConfiguration={
                "scanOnPush": scan_on_push
            }
        )
        eh.add_log("Updated Image Scanning Configuration", response)
    
    except ClientError as e:
        handle_common_errors(
            e, eh, "Update Image Scanning Configuration Failed", 30,
            perm_errors=[
                "RepositoryNotFoundException",
                "InvalidParameterException",
                "ValidationException"
            ]
        )

@ext(handler=eh, op="update_image_tag_mutability")
def update_image_tag_mutability(name, image_tag_mutability):
    registry_id = eh.props.get("registry_id")

    try:
        response = ecr.put_image_tag_mutability(
            registryId=registry_id,
            repositoryName=name,
            imageTagMutability=image_tag_mutability
        )
        eh.add_log("Updated Image Tag Mutability", response)
    
    except ClientError as e:
        handle_common_errors(
            e, eh, "Update Image Tag Mutability Failed", 35,
            perm_errors=[
                "RepositoryNotFoundException",
                "InvalidParameterException"
            ]
        )

@ext(handler=eh, op="delete_repository")
def delete_repository():
    repo_name = eh.ops['delete_repository'].get("name")
    car = eh.ops['delete_repository'].get("create_and_remove")
    registry_id = eh.ops['delete_repository'].get("registry_id")

    try:
        params = remove_none_attributes({
            "repositoryName": repo_name,
            "registryId": registry_id,
            "force": True
        })

        _ = ecr.delete_repository(**params)
        eh.add_log("Deleted Repo if it Existed", {"name": repo_name})

    except botocore.exceptions.ClientError as e:
        if e.response['Error']['Code'] == "RepositoryNotFoundException":
            eh.add_log("Old Repo Does Not Exist", {"name": repo_name})
        else:
            handle_common_errors(e, eh, "Delete Repo Failed", 80 if car else 10)

@ext(handler=eh, op="add_tags")
def add_tags():
    tags = format_tags(eh.ops['add_tags'])
    arn = eh.props['arn']

    try:
        ecr.tag_resource(
            ResourceArn=arn,
            Tags=tags
        )
        eh.add_log("Tags Added", {"tags": tags})

    except ClientError as e:
        handle_common_errors(e, eh, "Add Tags Failed", 50, ['InvalidParameterValueException'])
        
@ext(handler=eh, op="remove_tags")
def remove_tags():
    arn = eh.props['arn']

    try:
        ecr.untag_resource(
            ResourceArn=arn,
            TagKeys=eh.ops['remove_tags']
        )
        eh.add_log("Tags Removed", {"tags": eh.ops['remove_tags']})

    except botocore.exceptions.ClientError as e:
        handle_common_errors(e, eh, "Remove Tags Failed", 65, ['InvalidParameterValueException'])

def format_tags(tags_dict):
    return [{"Key": k, "Value": v} for k,v in tags_dict]

def unformat_tags(tags_list):
    return {t["Key"]: t["Value"] for t in tags_list}

def gen_codebuild_arn(codebuild_project_name, region, account_number):
    return f"arn:aws:codebuild:{region}:{account_number}:project/{codebuild_project_name}"

def gen_codebuild_link(codebuild_project_name):
    return f"https://console.aws.amazon.com/codesuite/codebuild/projects/{codebuild_project_name}"



