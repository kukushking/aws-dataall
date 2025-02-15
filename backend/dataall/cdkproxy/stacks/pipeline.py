import logging
import os
import shutil
import subprocess
from typing import List


from aws_cdk import aws_codebuild as codebuild, Stack, RemovalPolicy, CfnOutput
from aws_cdk import aws_codecommit as codecommit
from aws_cdk import aws_codepipeline as codepipeline
from aws_cdk import aws_codepipeline_actions as codepipeline_actions

from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms

from aws_cdk.aws_s3_assets import Asset
from botocore.exceptions import ClientError

from .manager import stack
from ...aws.handlers.sts import SessionHelper
from ... import db
from ...db import models
from ...db.api import Environment, Pipeline, Dataset
from ...utils.cdk_nag_utils import CDKNagUtil
from ...utils.runtime_stacks_tagging import TagsUtil

logger = logging.getLogger(__name__)


@stack("pipeline")
class PipelineStack(Stack):
    """
    Create a stack that contains CDK Continuous Integration and Delivery (CI/CD) pipeline.

    The pipeline is based on CodePipeline pipelines

    - Defaults for source/synth - CodeCommit & cdk synth
    - blueprint with DDK application code added in the CodeCommit repository <https://github.com/awslabs/aws-ddk>
    - ability to define development stages: dev, test, prod
    - ability to select gitflow or trunk-based as development strategy
    - Ability to connect to private artifactory to pull artifacts from at synth
    - Security best practices - ensures pipeline buckets block non-SSL, and are KMS-encrypted with rotated keys
    - data.all metadata as environment variables accesible at synth

    """

    module_name = __file__

    def get_engine(self):
        envname = os.environ.get("envname", "local")
        engine = db.get_engine(envname=envname)
        return engine

    def get_target(self, target_uri) -> models.DataPipeline:
        engine = self.get_engine()
        with engine.scoped_session() as session:
            return Pipeline.get_pipeline_by_uri(session, target_uri)

    def get_pipeline_environments(self, targer_uri) -> models.DataPipelineEnvironment:
        engine = self.get_engine()
        with engine.scoped_session() as session:
            envs = Pipeline.query_pipeline_environments(
                session, targer_uri
            )
        return envs

    def get_pipeline_cicd_environment(
        self, pipeline: models.DataPipeline
    ) -> models.Environment:
        envname = os.environ.get("envname", "local")
        engine = db.get_engine(envname=envname)
        with engine.scoped_session() as session:
            return Environment.get_environment_by_uri(session, pipeline.environmentUri)

    def get_env_team(self, pipeline: models.DataPipeline) -> models.EnvironmentGroup:
        engine = self.get_engine()
        with engine.scoped_session() as session:
            env = Environment.get_environment_group(
                session, pipeline.SamlGroupName, pipeline.environmentUri
            )
        return env

    def get_dataset(self, dataset_uri) -> models.Dataset:
        engine = self.get_engine()
        with engine.scoped_session() as session:
            ds = Dataset.get_dataset_by_uri(
                session, dataset_uri
            )
        return ds

    def __init__(self, scope, id, target_uri: str = None, **kwargs):
        kwargs.setdefault("tags", {}).update({"utility": "dataall-data-pipeline"})
        super().__init__(
            scope,
            id,
            env=kwargs.get("env"),
            stack_name=kwargs.get("stack_name"),
            tags=kwargs.get("tags"),
            description="Cloud formation stack of PIPELINE: {}; URI: {}; DESCRIPTION: {}".format(
                self.get_target(target_uri=target_uri).label,
                target_uri,
                self.get_target(target_uri=target_uri).description,
            )[
                :1024
            ],
        )

        # Configuration
        self.target_uri = target_uri

        pipeline = self.get_target(target_uri=target_uri)
        pipeline_environment = self.get_pipeline_cicd_environment(pipeline=pipeline)
        pipeline_env_team = self.get_env_team(pipeline=pipeline)
        # Development environments
        development_environments = self.get_pipeline_environments(targer_uri=target_uri)
        self.devStages = [env.stage for env in development_environments]

        # Support resources
        build_role_policy = iam.Policy(
            self,
            f"{pipeline.name}-policy",
            policy_name=f"{pipeline.name}-policy",
            statements=self.make_codebuild_policy_statements(
                pipeline_environment=pipeline_environment,
                pipeline_env_team=pipeline_env_team,
                pipeline=pipeline
            ),
        )

        build_project_role = iam.Role(
            self,
            "PipelineRole",
            role_name=pipeline.name,
            inline_policies={f"Inline{pipeline.name}": build_role_policy.document},
            assumed_by=iam.ServicePrincipal("codebuild.amazonaws.com"),
        )

        self.codebuild_key = kms.Key(
            self,
            f"{pipeline.name}-codebuild-key",
            removal_policy=RemovalPolicy.DESTROY,
            alias=f"{pipeline.name}-codebuild-key",
            enable_key_rotation=True,
            policy=iam.PolicyDocument(
                statements=[
                    iam.PolicyStatement(
                        resources=["*"],
                        effect=iam.Effect.ALLOW,
                        principals=[
                            iam.AccountPrincipal(account_id=self.account),
                        ],
                        actions=["kms:*"],
                    ),
                    iam.PolicyStatement(
                        resources=["*"],
                        effect=iam.Effect.ALLOW,
                        principals=[
                            iam.ServicePrincipal(service="codebuild.amazonaws.com"),
                        ],
                        actions=["kms:GenerateDataKey*", "kms:Decrypt"],
                    ),
                ],
            ),
        )

        # Create CodeCommit repository and mirror blueprint code
        code_dir_path = os.path.realpath(
            os.path.abspath(
                os.path.join(
                    __file__, "..", "..", "..", "..", "blueprints", "data_pipeline_blueprint"
                )
            )
        )

        try:
            env_vars, aws = PipelineStack._set_env_vars(pipeline_environment)
            codecommit_client = aws.client('codecommit', region_name=pipeline_environment.region)
            repository = PipelineStack._check_repository(codecommit_client, pipeline.repo)
            if repository:
                PipelineStack.write_ddk_json_multienvironment(path=code_dir_path, output_file="ddk.json", pipeline_environment=pipeline_environment, development_environments=development_environments)

                logger.info(f"Pipeline Repo {pipeline.repo} Exists...Handling Update")
                update_cmds = [
                    f'REPO_NAME={pipeline.repo}',
                    'COMMITID=$(aws codecommit get-branch --repository-name ${REPO_NAME} --branch-name main --query branch.commitId --output text)',
                    'aws codecommit put-file --repository-name ${REPO_NAME} --branch-name main --file-content file://ddk.json --file-path ddk.json --parent-commit-id ${COMMITID} --cli-binary-format raw-in-base64-out',
                ]

                process = subprocess.run(
                    "; ".join(update_cmds),
                    text=True,
                    shell=True,  # nosec
                    encoding='utf-8',
                    cwd=code_dir_path,
                    env=env_vars
                )
            else:
                raise Exception
        except Exception as e:
            PipelineStack.initialize_repo(pipeline, code_dir_path)

            PipelineStack.write_deploy_buildspec(path=code_dir_path, output_file=f"{pipeline.repo}/deploy_buildspec.yaml")

            PipelineStack.write_ddk_json_multienvironment(path=code_dir_path, output_file=f"{pipeline.repo}/ddk.json", pipeline_environment=pipeline_environment, development_environments=development_environments)

            logger.info(f"Pipeline Repo {pipeline.repo} Does Not Exists... Creating Repository")

            PipelineStack.cleanup_zip_directory(code_dir_path)

            PipelineStack.zip_directory(os.path.join(code_dir_path, pipeline.repo))
            code_asset = Asset(
                scope=self, id=f"{pipeline.name}-asset", path=f"{code_dir_path}/{pipeline.repo}/code.zip"
            )

            code = codecommit.CfnRepository.CodeProperty(
                s3=codecommit.CfnRepository.S3Property(
                    bucket=code_asset.s3_bucket_name,
                    key=code_asset.s3_object_key,
                )
            )

            repository = codecommit.CfnRepository(
                scope=self,
                code=code,
                id="CodecommitRepository",
                repository_name=pipeline.repo,
            )
            repository.apply_removal_policy(RemovalPolicy.RETAIN)

        if pipeline.devStrategy == "trunk":
            codepipeline_pipeline = codepipeline.Pipeline(
                scope=self,
                id=pipeline.name,
                pipeline_name=pipeline.name,
                restart_execution_on_update=True,
            )
            self.codepipeline_pipeline = codepipeline_pipeline
            self.source_artifact = codepipeline.Artifact()

            codepipeline_pipeline.add_stage(
                stage_name='Source',
                actions=[
                    codepipeline_actions.CodeCommitSourceAction(
                        action_name='CodeCommit',
                        branch='main',
                        output=self.source_artifact,
                        trigger=codepipeline_actions.CodeCommitTrigger.POLL,
                        repository=codecommit.Repository.from_repository_name(
                            self, 'source_blueprint_repo', repository_name=pipeline.repo
                        ),
                    )
                ],
            )

            for env in sorted(development_environments, key=lambda env: env.order):
                buildspec = "deploy_buildspec.yaml"
                build_project = codebuild.PipelineProject(
                    scope=self,
                    id=f'{pipeline.name}-build-{env.stage}',
                    environment=codebuild.BuildEnvironment(
                        privileged=True,
                        build_image=codebuild.LinuxBuildImage.AMAZON_LINUX_2_3,
                        environment_variables=PipelineStack.make_environment_variables(
                            pipeline=pipeline,
                            pipeline_environment=env,
                            pipeline_env_team=env.samlGroupName,
                            stage=env.stage,
                            stages=self.devStages
                        ),
                    ),
                    role=build_project_role,
                    build_spec=codebuild.BuildSpec.from_source_filename(buildspec),
                    encryption_key=self.codebuild_key,
                )

                self.codepipeline_pipeline.add_stage(
                    stage_name=f'Deploy-Stage-{env.stage}',
                    actions=[
                        codepipeline_actions.CodeBuildAction(
                            action_name=f'deploy-{env.stage}',
                            input=self.source_artifact,
                            project=build_project,
                            outputs=[codepipeline.Artifact()],
                        )
                    ],
                )

                # Skip manual approval for one stage pipelines and for last stage
                if env.order < development_environments.count():
                    self.codepipeline_pipeline.add_stage(
                        stage_name=f'ManualApproval-{env.stage}',
                        actions=[
                            codepipeline_actions.ManualApprovalAction(
                                action_name=f'ManualApproval-{env.stage}'
                            )
                        ],
                    )

        else:
            for env in development_environments:
                branch_name = 'main' if (env.stage == 'prod') else env.stage
                buildspec = "deploy_buildspec.yaml"

                codepipeline_pipeline = codepipeline.Pipeline(
                    scope=self,
                    id=f"{pipeline.name}-{env.stage}",
                    pipeline_name=f"{pipeline.name}-{env.stage}",
                    restart_execution_on_update=True,
                )
                self.codepipeline_pipeline = codepipeline_pipeline
                self.source_artifact = codepipeline.Artifact()

                codepipeline_pipeline.add_stage(
                    stage_name=f'Source-{env.stage}',
                    actions=[
                        codepipeline_actions.CodeCommitSourceAction(
                            action_name='CodeCommit',
                            branch=branch_name,
                            output=self.source_artifact,
                            trigger=codepipeline_actions.CodeCommitTrigger.POLL,
                            repository=codecommit.Repository.from_repository_name(
                                self, f'source_blueprint_repo_{env.stage}', repository_name=pipeline.repo
                            ),
                        )
                    ],
                )

                build_project = codebuild.PipelineProject(
                    scope=self,
                    id=f'{pipeline.name}-build-{env.stage}',
                    environment=codebuild.BuildEnvironment(
                        privileged=True,
                        build_image=codebuild.LinuxBuildImage.AMAZON_LINUX_2_3,
                        environment_variables=PipelineStack.make_environment_variables(
                            pipeline=pipeline,
                            pipeline_environment=env,
                            pipeline_env_team=env.samlGroupName,
                            stage=env.stage,
                            stages=self.devStages
                        ),
                    ),
                    role=build_project_role,
                    build_spec=codebuild.BuildSpec.from_source_filename(buildspec),
                    encryption_key=self.codebuild_key,
                )

                self.codepipeline_pipeline.add_stage(
                    stage_name=f'Deploy-Stage-{env.stage}',
                    actions=[
                        codepipeline_actions.CodeBuildAction(
                            action_name=f'deploy-{env.stage}',
                            input=self.source_artifact,
                            project=build_project,
                            outputs=[codepipeline.Artifact()],
                        )
                    ],
                )

        # CloudFormation output
        CfnOutput(
            self,
            "RepoNameOutput",
            export_name=f"{pipeline.DataPipelineUri}-RepositoryName",
            value=pipeline.repo,
        )
        CfnOutput(
            self,
            "PipelineNameOutput",
            export_name=f"{pipeline.DataPipelineUri}-PipelineName",
            value=codepipeline_pipeline.pipeline_name,
        )

        TagsUtil.add_tags(self)

        CDKNagUtil.check_rules(self)

        PipelineStack.cleanup_zip_directory(code_dir_path)
        PipelineStack.cleanup_pipeline_directory(os.path.join(code_dir_path, pipeline.repo))

    @staticmethod
    def zip_directory(path):
        try:
            shutil.make_archive("code", "zip", path)
            shutil.move("code.zip", f"{path}/code.zip")
        except Exception as e:
            logger.error(f"Failed to zip repository due to: {e}")

    @staticmethod
    def cleanup_zip_directory(path):
        if os.path.isfile(f"{path}/code.zip"):
            os.remove(f"{path}/code.zip")
        else:
            logger.info("Info: %s Zip not found" % f"{path}/code.zip")

    @staticmethod
    def cleanup_pipeline_directory(path):
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            logger.info("Info: %s Directory not found" % f"{path}")

    @staticmethod
    def make_environment_variables(
        pipeline,
        pipeline_environment,
        pipeline_env_team,
        stage,
        stages
    ):

        env_vars_1 = {
            "PIPELINE_URI": codebuild.BuildEnvironmentVariable(value=pipeline.DataPipelineUri),
            "PIPELINE_NAME": codebuild.BuildEnvironmentVariable(value=pipeline.name),
            "STAGE": codebuild.BuildEnvironmentVariable(value=stage),
            "DEV_STAGES": codebuild.BuildEnvironmentVariable(value=stages),
            "DEV_STRATEGY": codebuild.BuildEnvironmentVariable(value=pipeline.devStrategy),
            "TEMPLATE": codebuild.BuildEnvironmentVariable(value=pipeline.template),
            "ENVIRONMENT_URI": codebuild.BuildEnvironmentVariable(value=pipeline_environment.environmentUri),
            "AWSACCOUNTID": codebuild.BuildEnvironmentVariable(value=pipeline_environment.AwsAccountId),
            "AWSREGION": codebuild.BuildEnvironmentVariable(value=pipeline_environment.region),
            "ENVTEAM_ROLENAME": codebuild.BuildEnvironmentVariable(value=pipeline_env_team),
        }
        env_vars = dict(env_vars_1)
        return env_vars

    @staticmethod
    def write_deploy_buildspec(path, output_file):
        yaml = """
            version: '0.2'
            env:
                git-credential-helper: yes
            phases:
              pre_build:
                commands:
                - n 16.15.1
                - npm install -g aws-cdk
                - pip install aws-ddk
                - pip install -r requirements.txt
              build:
                commands:
                    - aws sts get-caller-identity
                    - ddk deploy
        """
        with open(f'{path}/{output_file}', 'x') as text_file:
            print(yaml, file=text_file)

    @staticmethod
    def make_codebuild_policy_statements(
            pipeline_environment,
            pipeline_env_team,
            pipeline
    ) -> List[iam.PolicyStatement]:
        return [
            iam.PolicyStatement(
                actions=[
                    "ec2:DescribeAvailabilityZones",
                    "kms:Decrypt",
                    "kms:Encrypt",
                    "kms:GenerateDataKey",
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:DescribeSecret",
                    "ssm:GetParametersByPath",
                    "ssm:GetParameters",
                    "ssm:GetParameter",
                    "codebuild:CreateReportGroup",
                    "codebuild:CreateReport",
                    "codebuild:UpdateReport",
                    "codebuild:BatchPutTestCases",
                    "codebuild:BatchPutCodeCoverages",
                    "codecommit:ListRepositories",
                    "sts:AssumeRole",
                    "cloudformation:DescribeStacks"
                ],
                resources=["*"],
            ),
            iam.PolicyStatement(
                actions=[
                    "codecommit:*"
                ],
                resources=[f"arn:aws:codecommit:{pipeline_environment.region}:{pipeline_environment.AwsAccountId}:{pipeline.repo}"],
            )
        ]

    @staticmethod
    def write_ddk_json_multienvironment(path, output_file, pipeline_environment, development_environments):
        json_envs = ""
        for env in development_environments:
            json_env = f""",
        "{env.stage}": {{
            "account": "{env.AwsAccountId}",
            "region": "{env.region}",
            "stage": "{env.stage}",
            "env_vars": {{
                "database": "example_database",
                "Team": "{env.samlGroupName}"
            }}
        }}"""
            json_envs = json_envs + json_env

        json = f"""{{
    "environments": {{
        "cicd": {{
            "account": "{pipeline_environment.AwsAccountId}",
            "region": "{pipeline_environment.region}",
            "stage": "cicd"
        }}{json_envs}
    }}
}}"""

        with open(f'{path}/{output_file}', 'w') as text_file:
            print(json, file=text_file)

    def initialize_repo(pipeline, code_dir_path):

        venv_name = ".venv"

        cmd_init = [
            f"ddk init {pipeline.repo} --generate-only",
            f"cp app_multiaccount.py ./{pipeline.repo}/app.py",
            f"cp ddk_app/ddk_app_stack_multiaccount.py ./{pipeline.repo}/ddk_app/ddk_app_stack.py",
            f"mkdir ./{pipeline.repo}/utils",
            f"cp -R utils/* ./{pipeline.repo}/utils/"
        ]

        logger.info(f"Running Commands: {'; '.join(cmd_init)}")

        process = subprocess.run(
            '; '.join(cmd_init),
            text=True,
            shell=True,  # nosec
            encoding='utf-8',
            cwd=code_dir_path
        )
        if process.returncode == 0:
            logger.info("Successfully Initialized New CDK/DDK App")
            return

    @staticmethod
    def _set_env_vars(pipeline_environment):
        aws = SessionHelper.remote_session(pipeline_environment.AwsAccountId)
        env_creds = aws.get_credentials()

        env = {
            'AWS_REGION': pipeline_environment.region,
            'AWS_DEFAULT_REGION': pipeline_environment.region,
            'CURRENT_AWS_ACCOUNT': pipeline_environment.AwsAccountId,
            'envname': os.environ.get('envname', 'local'),
        }
        if env_creds:
            env.update(
                {
                    'AWS_ACCESS_KEY_ID': env_creds.access_key,
                    'AWS_SECRET_ACCESS_KEY': env_creds.secret_key,
                    'AWS_SESSION_TOKEN': env_creds.token
                }
            )
        return env, aws

    @staticmethod
    def _check_repository(codecommit_client, repo_name):
        repository = None
        logger.info(f"Checking Repository Exists: {repo_name}")
        try:
            repository = codecommit_client.get_repository(repositoryName=repo_name)
        except ClientError as e:
            if e.response['Error']['Code'] == 'RepositoryDoesNotExistException':
                logger.debug(f'Repository does not exists {repo_name} %s', e)
            else:
                raise e
        return repository if repository else None
