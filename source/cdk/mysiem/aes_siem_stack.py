# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import boto3
import botocore
from botocore.exceptions import ClientError
from aws_cdk import (
    aws_cloudformation,
    aws_ec2,
    aws_events,
    aws_events_targets,
    aws_iam,
    aws_kinesis,
    aws_kms,
    aws_lambda,
    aws_lambda_event_sources,
    aws_logs,
    aws_s3,
    aws_s3_notifications,
    aws_sns,
    aws_sns_subscriptions,
    aws_sqs,
    core,
    region_info,
)

__version__ = '2.1.0-beta4'
print(__version__)

iam_client = boto3.client('iam')
ec2_resource = boto3.resource('ec2')
ec2_client = boto3.resource('ec2')


def validate_cdk_json(context):
    print('\ncdk.json validation for vpc configuration is starting...\n')
    vpc_type = context.node.try_get_context("vpc_type")
    if vpc_type == 'new':
        print('vpc_type:\t\t\tnew')
        return True
    elif vpc_type == 'import':
        print('vpc_type:\t\t\timport')
    else:
        raise Exception('vpc_type is invalid. You can use "new" or "import". '
                        'Exit. Fix and Try again')

    vpcid = context.node.try_get_context("imported_vpc_id")
    vpc_client = ec2_resource.Vpc(vpcid)
    print('checking vpc...')
    vpc_client.state
    print(f'checking vpc id...:\t\t{vpcid}')
    is_dns_support = vpc_client.describe_attribute(
        Attribute='enableDnsSupport')['EnableDnsSupport']['Value']
    print(f'checking dns support...:\t{is_dns_support}')
    is_dns_hotname = vpc_client.describe_attribute(
        Attribute='enableDnsHostnames')['EnableDnsHostnames']['Value']
    print(f'checking dns hostname...:\t{is_dns_hotname}')
    if not is_dns_support or not is_dns_hotname:
        raise Exception('enable DNS Hostname and DNS Support. Exit...')
    print('checking vpc is...\t\t[PASS]\n')

    subnet_ids_from_the_vpc = []
    subnet_objs_from_the_vpc = vpc_client.subnets.all()
    for subnet_obj in subnet_objs_from_the_vpc:
        subnet_ids_from_the_vpc.append(subnet_obj.id)

    def get_pub_or_priv_subnet(routes_attrs):
        for route in routes_attrs:
            if route['GatewayId'].startswith('igw-'):
                return 'public'
        return 'private'

    validation_result = True
    subnet_types = {}
    routetables = vpc_client.route_tables.all()
    for routetable in routetables:
        rt_client = ec2_resource.RouteTable(routetable.id)
        subnet_type = get_pub_or_priv_subnet(rt_client.routes_attribute)
        for attribute in rt_client.associations_attribute:
            subnetid = attribute.get('SubnetId', "")
            main = attribute.get('Main', "")
            if subnetid:
                subnet_types[subnetid] = subnet_type
            elif main:
                subnet_types['main'] = subnet_type

    print('checking subnet...')
    subnet_ids = get_subnet_ids(context)

    for subnet_id in subnet_ids:
        if subnet_id in subnet_ids_from_the_vpc:
            if subnet_id in subnet_types:
                subnet_type = subnet_types[subnet_id]
            else:
                subnet_type = subnet_types['main']
            if subnet_type == 'private':
                print(f'{subnet_id} is\tprivate')
            elif subnet_type == 'public':
                print(f'{subnet_id} is\tpublic')
                validation_result = False
        else:
            print(f'{subnet_id} is\tnot exist')
            validation_result = False
    if not validation_result:
        raise Exception('subnet is invalid. Modify it.')
    print('checking subnet is...\t\t[PASS]\n')
    print('IGNORE Following Warning. '
          '"No routeTableId was provided to the subnet..."')


def get_subnet_ids(context):
    subnet_ids = []
    subnet_ids = context.node.try_get_context('imported_vpc_subnets')
    if not subnet_ids:
        # compatibility for v2.0.0
        sbunet1 = context.node.try_get_context('imported_vpc_subnet1')
        sbunet2 = context.node.try_get_context('imported_vpc_subnet2')
        sbunet3 = context.node.try_get_context('imported_vpc_subnet3')
        subnet_ids = [sbunet1['subnet_id'], sbunet2['subnet_id'],
                      sbunet3['subnet_id']]
    return subnet_ids


def check_iam_role(pathprefix):
    role_iterator = iam_client.list_roles(PathPrefix=pathprefix)
    if len(role_iterator['Roles']) == 1:
        return True
    else:
        return False


class MyAesSiemStack(core.Stack):

    def __init__(self, scope: core.Construct, id: str, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        if self.node.try_get_context('vpc_type'):
            validate_cdk_json(self)

        ES_LOADER_TIMEOUT = 600
        ######################################################################
        # ELB mapping
        ######################################################################
        elb_id_temp = region_info.FactName.ELBV2_ACCOUNT
        elb_map_temp = region_info.RegionInfo.region_map(elb_id_temp)
        elb_mapping = {}
        for key in elb_map_temp:
            elb_mapping[key] = {'accountid': elb_map_temp[key]}
        elb_accounts = core.CfnMapping(
            scope=self, id='ELBv2AccountMap', mapping=elb_mapping)
        ######################################################################
        # get params
        ######################################################################
        allow_source_address = core.CfnParameter(
            self, 'AllowedSourceIpAddresses', allowed_pattern=r'^[0-9./\s]*',
            description='Space-delimited list of CIDR blocks',
            default='10.0.0.0/8 172.16.0.0/12 192.168.0.0/16')
        sns_email = core.CfnParameter(
            self, 'SnsEmail', allowed_pattern=r'^[0-9a-zA-Z@_\-\+\.]*',
            description=('Input your email as SNS topic, where Amazon ES will '
                         'send alerts to'),
            default='user+sns@example.com')
        geoip_license_key = core.CfnParameter(
            self, 'GeoLite2LicenseKey', allowed_pattern=r'^[0-9a-zA-Z]{16}$',
            default='xxxxxxxxxxxxxxxx',
            description=("If you wolud like to enrich geoip locaiton such as "
                         "IP address's country, get a license key form MaxMind"
                         " and input the key. If you not, keep "
                         "xxxxxxxxxxxxxxxx"))

        aes_domain_name = self.node.try_get_context('aes_domain_name')
        bucket = f'{aes_domain_name}-{core.Aws.ACCOUNT_ID}'
        s3bucket_name_geo = f'{bucket}-geo'
        s3bucket_name_log = f'{bucket}-log'
        s3bucket_name_snapshot = f'{bucket}-snapshot'

        # organizations / multiaccount
        org_id = self.node.try_get_context('organizations').get('org_id')
        org_mgmt_id = self.node.try_get_context(
            'organizations').get('management_id')
        org_member_ids = self.node.try_get_context(
            'organizations').get('member_ids')
        no_org_ids = self.node.try_get_context(
            'no_organizations').get('aws_accounts')

        temp_geo = self.node.try_get_context('s3_bucket_name').get('geo')
        if temp_geo:
            s3bucket_name_geo = temp_geo
        temp_log = self.node.try_get_context('s3_bucket_name').get('log')
        if temp_log:
            s3bucket_name_log = temp_log
        elif org_id or no_org_ids:
            s3bucket_name_log = f'{aes_domain_name}-{self.account}-log'
        temp_snap = self.node.try_get_context('s3_bucket_name').get('snapshot')
        if temp_snap:
            s3bucket_name_snapshot = temp_snap

        kms_cmk_alias = self.node.try_get_context('kms_cmk_alias')
        if not kms_cmk_alias:
            kms_cmk_alias = 'aes-siem-key'

        ######################################################################
        # deploy VPC when context is defined as using VPC
        ######################################################################
        # vpc_type is 'new' or 'import' or None
        vpc_type = self.node.try_get_context('vpc_type')

        if vpc_type == 'new':
            is_vpc = True
            vpc_cidr = self.node.try_get_context('new_vpc_nw_cidr_block')
            subnet_cidr_mask = int(
                self.node.try_get_context('new_vpc_subnet_cidr_mask'))
            is_vpc = True
            # VPC
            vpc_aes_siem = aws_ec2.Vpc(
                self, 'VpcAesSiem', cidr=vpc_cidr,
                max_azs=3, nat_gateways=0,
                subnet_configuration=[
                    aws_ec2.SubnetConfiguration(
                        subnet_type=aws_ec2.SubnetType.ISOLATED,
                        name='aes-siem-subnet', cidr_mask=subnet_cidr_mask)])
            subnet1 = vpc_aes_siem.isolated_subnets[0]
            subnets = [{'subnet_type': aws_ec2.SubnetType.ISOLATED}]
            vpc_subnets = aws_ec2.SubnetSelection(
                subnet_type=aws_ec2.SubnetType.ISOLATED)
            vpc_aes_siem_opt = vpc_aes_siem.node.default_child.cfn_options
            vpc_aes_siem_opt.deletion_policy = core.CfnDeletionPolicy.RETAIN
            for subnet in vpc_aes_siem.isolated_subnets:
                subnet_opt = subnet.node.default_child.cfn_options
                subnet_opt.deletion_policy = core.CfnDeletionPolicy.RETAIN
        elif vpc_type == 'import':
            vpc_id = self.node.try_get_context('imported_vpc_id')
            vpc_aes_siem = aws_ec2.Vpc.from_lookup(
                self, 'VpcAesSiem', vpc_id=vpc_id)

            subnet_ids = get_subnet_ids(self)
            subnets = []
            for number, subnet_id in enumerate(subnet_ids, 1):
                obj_id = 'Subenet' + str(number)
                subnet = aws_ec2.Subnet.from_subnet_id(self, obj_id, subnet_id)
                subnets.append(subnet)
            subnet1 = subnets[0]
            vpc_subnets = aws_ec2.SubnetSelection(subnets=subnets)

        if vpc_type:
            is_vpc = True
            # Security Group
            sg_vpc_noinbound_aes_siem = aws_ec2.SecurityGroup(
                self, 'AesSiemVpcNoinboundSecurityGroup',
                security_group_name='aes-siem-noinbound-vpc-sg',
                vpc=vpc_aes_siem)

            sg_vpc_aes_siem = aws_ec2.SecurityGroup(
                self, 'AesSiemVpcSecurityGroup',
                security_group_name='aes-siem-vpc-sg',
                vpc=vpc_aes_siem)
            sg_vpc_aes_siem.add_ingress_rule(
                peer=aws_ec2.Peer.ipv4(vpc_aes_siem.vpc_cidr_block),
                connection=aws_ec2.Port.tcp(443),)
            sg_vpc_opt = sg_vpc_aes_siem.node.default_child.cfn_options
            sg_vpc_opt.deletion_policy = core.CfnDeletionPolicy.RETAIN

            # VPC Endpoint
            vpc_aes_siem.add_gateway_endpoint(
                'S3Endpoint', service=aws_ec2.GatewayVpcEndpointAwsService.S3,
                subnets=subnets)
            vpc_aes_siem.add_interface_endpoint(
                'SQSEndpoint', security_groups=[sg_vpc_aes_siem],
                service=aws_ec2.InterfaceVpcEndpointAwsService.SQS,)
            vpc_aes_siem.add_interface_endpoint(
                'KMSEndpoint', security_groups=[sg_vpc_aes_siem],
                service=aws_ec2.InterfaceVpcEndpointAwsService.KMS,)
            vpc_aes_siem.add_interface_endpoint(
                'SNSEndpoint', security_groups=[sg_vpc_aes_siem],
                service=aws_ec2.InterfaceVpcEndpointAwsService.SNS,)
        else:
            is_vpc = False

        is_vpc = core.CfnCondition(
            self, 'IsVpc', expression=core.Fn.condition_equals(is_vpc, True))
        """
        CloudFormation実行時の条件式の書き方
        ClassのBasesが aws_cdk.core.Resource の時は、
        node.default_child.cfn_options.condition = is_vpc
        ClassのBasesが aws_cdk.core.CfnResource の時は、
        cfn_options.condition = is_vpc
        """

        ######################################################################
        # create cmk of KMS to encrypt S3 bucket
        ######################################################################
        kms_aes_siem = aws_kms.Key(
            self, 'KmsAesSiemLog', description='CMK for SIEM solution',
            removal_policy=core.RemovalPolicy.RETAIN)

        aws_kms.Alias(
            self, 'KmsAesSiemLogAlias', alias_name=kms_cmk_alias,
            target_key=kms_aes_siem,
            removal_policy=core.RemovalPolicy.RETAIN)

        kms_aes_siem.add_to_resource_policy(
            aws_iam.PolicyStatement(
                sid='Allow GuardDuty to use the key',
                actions=['kms:GenerateDataKey'],
                principals=[aws_iam.ServicePrincipal(
                    'guardduty.amazonaws.com')],
                resources=['*'],),)

        kms_aes_siem.add_to_resource_policy(
            aws_iam.PolicyStatement(
                sid='Allow VPC Flow Logs to use the key',
                actions=['kms:Encrypt', 'kms:Decrypt', 'kms:ReEncrypt*',
                         'kms:GenerateDataKey*', 'kms:DescribeKey'],
                principals=[aws_iam.ServicePrincipal(
                    'delivery.logs.amazonaws.com')],
                resources=['*'],),)
        # basic policy
        key_policy_basic1 = aws_iam.PolicyStatement(
            sid='Allow principals in the account to decrypt log files',
            actions=['kms:DescribeKey', 'kms:ReEncryptFrom'],
            principals=[aws_iam.AccountPrincipal(
                account_id=core.Aws.ACCOUNT_ID)],
            resources=['*'],)
        kms_aes_siem.add_to_resource_policy(key_policy_basic1)

        # for CloudTrail
        key_policy_trail1 = aws_iam.PolicyStatement(
            sid='Allow CloudTrail to describe key',
            actions=['kms:DescribeKey'],
            principals=[aws_iam.ServicePrincipal('cloudtrail.amazonaws.com')],
            resources=['*'],)
        kms_aes_siem.add_to_resource_policy(key_policy_trail1)

        key_policy_trail2 = aws_iam.PolicyStatement(
            sid=('Allow CloudTrail to encrypt logs'),
            actions=['kms:GenerateDataKey*'],
            principals=[aws_iam.ServicePrincipal(
                'cloudtrail.amazonaws.com')],
            resources=['*'],
            conditions={'StringLike': {
                'kms:EncryptionContext:aws:cloudtrail:arn': [
                    f'arn:aws:cloudtrail:*:{core.Aws.ACCOUNT_ID}:trail/*']}})
        kms_aes_siem.add_to_resource_policy(key_policy_trail2)

        ######################################################################
        # create s3 bucket
        ######################################################################
        block_pub = aws_s3.BlockPublicAccess(
            block_public_acls=True,
            ignore_public_acls=True,
            block_public_policy=True,
            restrict_public_buckets=True
        )
        s3_geo = aws_s3.Bucket(
            self, 'S3BucketForGeoip', block_public_access=block_pub,
            bucket_name=s3bucket_name_geo,
            # removal_policy=core.RemovalPolicy.DESTROY,
        )

        # create s3 bucket for log collector
        s3_log = aws_s3.Bucket(
            self, 'S3BucketForLog', block_public_access=block_pub,
            bucket_name=s3bucket_name_log, versioned=True,
            encryption=aws_s3.BucketEncryption.S3_MANAGED,
            # removal_policy=core.RemovalPolicy.DESTROY,
        )

        # create s3 bucket for aes snapshot
        s3_snapshot = aws_s3.Bucket(
            self, 'S3BucketForSnapshot', block_public_access=block_pub,
            bucket_name=s3bucket_name_snapshot,
            # removal_policy=core.RemovalPolicy.DESTROY,
        )

        ######################################################################
        # IAM Role
        ######################################################################
        # snaphot rule for AES
        policydoc_snapshot = aws_iam.PolicyDocument(
            statements=[
                aws_iam.PolicyStatement(
                    actions=['s3:ListBucket'],
                    resources=[s3_snapshot.bucket_arn]
                ),
                aws_iam.PolicyStatement(
                    actions=['s3:GetObject', 's3:PutObject',
                             's3:DeleteObject'],
                    resources=[s3_snapshot.bucket_arn + '/*']
                )
            ]
        )
        aes_siem_snapshot_role = aws_iam.Role(
            self, 'AesSiemSnapshotRole',
            role_name='aes-siem-snapshot-role',
            inline_policies=[policydoc_snapshot, ],
            assumed_by=aws_iam.ServicePrincipal('es.amazonaws.com')
        )

        policydoc_assume_snapshrole = aws_iam.PolicyDocument(
            statements=[
                aws_iam.PolicyStatement(
                    actions=['iam:PassRole'],
                    resources=[aes_siem_snapshot_role.role_arn]
                ),
            ]
        )

        aes_siem_deploy_role_for_lambda = aws_iam.Role(
            self, 'AesSiemDeployRoleForLambda',
            role_name='aes-siem-deploy-role-for-lambda',
            managed_policies=[
                aws_iam.ManagedPolicy.from_aws_managed_policy_name(
                    'AmazonESFullAccess'),
                aws_iam.ManagedPolicy.from_aws_managed_policy_name(
                    'service-role/AWSLambdaBasicExecutionRole'),
            ],
            inline_policies=[policydoc_assume_snapshrole, policydoc_snapshot],
            assumed_by=aws_iam.ServicePrincipal('lambda.amazonaws.com')
        )

        if vpc_type:
            aes_siem_deploy_role_for_lambda.add_managed_policy(
                aws_iam.ManagedPolicy.from_aws_managed_policy_name(
                    'service-role/AWSLambdaVPCAccessExecutionRole')
            )

        # for alert from Amazon ES
        aes_siem_sns_role = aws_iam.Role(
            self, 'AesSiemSnsRole',
            role_name='aes-siem-sns-role',
            assumed_by=aws_iam.ServicePrincipal('es.amazonaws.com')
        )

        ######################################################################
        # in VPC
        ######################################################################
        aes_role_exist = check_iam_role('/aws-service-role/es.amazonaws.com/')
        if vpc_type and not aes_role_exist:
            slr_aes = aws_iam.CfnServiceLinkedRole(
                self, 'AWSServiceRoleForAmazonElasticsearchService',
                aws_service_name='es.amazonaws.com',
                description='Created by cloudformation of aes-siem stack'
            )
            slr_aes.cfn_options.deletion_policy = core.CfnDeletionPolicy.RETAIN

        ######################################################################
        # SQS for es-laoder's DLQ
        ######################################################################
        sqs_aes_siem_dlq = aws_sqs.Queue(
            self, 'AesSiemDlq', queue_name='aes-siem-dlq',
            retention_period=core.Duration.days(14))

        sqs_aes_siem_splitted_logs = aws_sqs.Queue(
            self, 'AesSiemSqsSplitLogs',
            queue_name='aes-siem-sqs-splitted-logs',
            dead_letter_queue=aws_sqs.DeadLetterQueue(
                max_receive_count=2, queue=sqs_aes_siem_dlq),
            visibility_timeout=core.Duration.seconds(ES_LOADER_TIMEOUT),
            retention_period=core.Duration.days(14))

        ######################################################################
        # Setup Lambda
        ######################################################################
        # setup lambda of es_loader
        lambda_es_loader_vpc_kwargs = {}
        if vpc_type:
            lambda_es_loader_vpc_kwargs = {
                'security_group': sg_vpc_noinbound_aes_siem,
                'vpc': vpc_aes_siem,
                'vpc_subnets': vpc_subnets,
            }

        lambda_es_loader = aws_lambda.Function(
            self, 'LambdaEsLoader', **lambda_es_loader_vpc_kwargs,
            function_name='aes-siem-es-loader',
            runtime=aws_lambda.Runtime.PYTHON_3_8,
            # code=aws_lambda.Code.asset('../lambda/es_loader.zip'),
            code=aws_lambda.Code.asset('../lambda/es_loader'),
            handler='index.lambda_handler',
            memory_size=2048,
            timeout=core.Duration.seconds(ES_LOADER_TIMEOUT),
            dead_letter_queue_enabled=True,
            dead_letter_queue=sqs_aes_siem_dlq,
            environment={'GEOIP_BUCKET': s3bucket_name_geo})
        es_loader_newver = lambda_es_loader.add_version(
            name=__version__, description=__version__)
        es_loader_opt = es_loader_newver.node.default_child.cfn_options
        es_loader_opt.deletion_policy = core.CfnDeletionPolicy.RETAIN

        # send only
        # sqs_aes_siem_dlq.grant(lambda_es_loader, 'sqs:SendMessage')
        # send and reieve. but it must be loop
        sqs_aes_siem_dlq.grant(
            lambda_es_loader, 'sqs:SendMessage', 'sqs:ReceiveMessage',
            'sqs:DeleteMessage', 'sqs:GetQueueAttributes')

        sqs_aes_siem_splitted_logs.grant(lambda_es_loader, 'sqs:SendMessage')
        sqs_aes_siem_splitted_logs.grant(
            lambda_es_loader, 'sqs:SendMessage', 'sqs:ReceiveMessage',
            'sqs:DeleteMessage', 'sqs:GetQueueAttributes')

        lambda_es_loader.add_event_source(
            aws_lambda_event_sources.SqsEventSource(
                sqs_aes_siem_splitted_logs, batch_size=1))

        lambda_geo = aws_lambda.Function(
            self, 'LambdaGeoipDownloader',
            function_name='aes-siem-geoip-downloader',
            runtime=aws_lambda.Runtime.PYTHON_3_8,
            code=aws_lambda.Code.asset('../lambda/geoip_downloader'),
            handler='index.lambda_handler',
            memory_size=320,
            timeout=core.Duration.seconds(300),
            environment={
                's3bucket_name': s3bucket_name_geo,
                'license_key': geoip_license_key.value_as_string,
            }
        )
        lambda_geo_newver = lambda_geo.add_version(
            name=__version__, description=__version__)
        lamba_geo_opt = lambda_geo_newver.node.default_child.cfn_options
        lamba_geo_opt.deletion_policy = core.CfnDeletionPolicy.RETAIN

        ######################################################################
        # setup elasticsearch
        ######################################################################
        lambda_deploy_es = aws_lambda.Function(
            self, 'LambdaDeployAES',
            function_name='aes-siem-deploy-aes',
            runtime=aws_lambda.Runtime.PYTHON_3_8,
            # code=aws_lambda.Code.asset('../lambda/deploy_es.zip'),
            code=aws_lambda.Code.asset('../lambda/deploy_es'),
            handler='index.aes_domain_handler',
            memory_size=128,
            timeout=core.Duration.seconds(720),
            environment={
                'accountid': core.Aws.ACCOUNT_ID,
                'aes_domain_name': aes_domain_name,
                'aes_admin_role': aes_siem_deploy_role_for_lambda.role_arn,
                'es_loader_role': lambda_es_loader.role.role_arn,
                'allow_source_address': allow_source_address.value_as_string,
            },
            role=aes_siem_deploy_role_for_lambda,
        )
        if vpc_type:
            lambda_deploy_es.add_environment(
                'vpc_subnet_id', subnet1.subnet_id)
            lambda_deploy_es.add_environment(
                'security_group_id', sg_vpc_aes_siem.security_group_id)
        else:
            lambda_deploy_es.add_environment('vpc_subnet_id', 'None')
            lambda_deploy_es.add_environment('security_group_id', 'None')
        deploy_es_newver = lambda_deploy_es.add_version(
            name=__version__, description=__version__)
        deploy_es_opt = deploy_es_newver.node.default_child.cfn_options
        deploy_es_opt.deletion_policy = core.CfnDeletionPolicy.RETAIN

        # execute lambda_deploy_es to deploy Amaozon ES Domain
        aes_domain = aws_cloudformation.CfnCustomResource(
            self, 'AesSiemDomainDeployedR2',
            service_token=lambda_deploy_es.function_arn,)
        aes_domain.add_override('Properties.ConfigVersion', __version__)
        aes_domain.cfn_options.deletion_policy = core.CfnDeletionPolicy.RETAIN

        es_endpoint = aes_domain.get_att('es_endpoint').to_string()
        lambda_es_loader.add_environment('ES_ENDPOINT', es_endpoint)
        lambda_es_loader.add_environment(
            'SQS_SPLITTED_LOGS_URL', sqs_aes_siem_splitted_logs.queue_url)

        lambda_configure_es_vpc_kwargs = {}
        if vpc_type:
            lambda_configure_es_vpc_kwargs = {
                'security_group': sg_vpc_noinbound_aes_siem,
                'vpc': vpc_aes_siem,
                'vpc_subnets': aws_ec2.SubnetSelection(subnets=[subnet1, ]), }
        lambda_configure_es = aws_lambda.Function(
            self, 'LambdaConfigureAES', **lambda_configure_es_vpc_kwargs,
            function_name='aes-siem-configure-aes',
            runtime=aws_lambda.Runtime.PYTHON_3_8,
            code=aws_lambda.Code.asset('../lambda/deploy_es'),
            handler='index.aes_config_handler',
            memory_size=128,
            timeout=core.Duration.seconds(300),
            environment={
                'accountid': core.Aws.ACCOUNT_ID,
                'aes_domain_name': aes_domain_name,
                'aes_admin_role': aes_siem_deploy_role_for_lambda.role_arn,
                'es_loader_role': lambda_es_loader.role.role_arn,
                'allow_source_address': allow_source_address.value_as_string,
                'es_endpoint': es_endpoint,
            },
            role=aes_siem_deploy_role_for_lambda,
        )
        if vpc_type:
            lambda_configure_es.add_environment(
                'vpc_subnet_id', subnet1.subnet_id)
            lambda_configure_es.add_environment(
                'security_group_id', sg_vpc_aes_siem.security_group_id)
        else:
            lambda_configure_es.add_environment('vpc_subnet_id', 'None')
            lambda_configure_es.add_environment('security_group_id', 'None')
        configure_es_newver = lambda_configure_es.add_version(
            name=__version__, description=__version__)
        configure_es_opt = configure_es_newver.node.default_child.cfn_options
        configure_es_opt.deletion_policy = core.CfnDeletionPolicy.RETAIN

        aes_config = aws_cloudformation.CfnCustomResource(
            self, 'AesSiemDomainConfiguredR2',
            service_token=lambda_configure_es.function_arn,
        )
        aes_config.add_override('Properties.ConfigVersion', __version__)
        aes_config.add_depends_on(aes_domain)
        aes_config.cfn_options.deletion_policy = core.CfnDeletionPolicy.RETAIN

        es_arn = (f'arn:aws:es:{core.Aws.REGION}:{core.Aws.ACCOUNT_ID}'
                  f':domain/{aes_domain_name}')
        # grant permission to es_loader role
        lambda_es_loader.role.attach_inline_policy(
            aws_iam.Policy(
                self, 'aes-siem-policy-to-load-entries-to-es',
                policy_name='aes-siem-policy-to-load-entries-to-es',
                statements=[
                    aws_iam.PolicyStatement(
                        actions=['es:*'],
                        resources=[es_arn + '/*', ]),
                ]
            )
        )
        # grant additional permission to es_loader role
        additional_kms_cmks = self.node.try_get_context('additional_kms_cmks')
        if additional_kms_cmks:
            lambda_es_loader.role.attach_inline_policy(
                aws_iam.Policy(
                    self, 'access_to_additional_cmks',
                    policy_name='access_to_additional_cmks',
                    statements=[
                        aws_iam.PolicyStatement(
                            actions=['kms:Decrypt'],
                            resources=sorted(set(additional_kms_cmks))
                        )
                    ]
                )
            )
        additional_buckets = self.node.try_get_context('additional_s3_buckets')
        if additional_buckets:
            buckets_list = []
            for bucket in additional_buckets:
                buckets_list.append(f'arn:aws:s3:::{bucket}')
                buckets_list.append(f'arn:aws:s3:::{bucket}/*')
            lambda_es_loader.role.attach_inline_policy(
                aws_iam.Policy(
                    self, 'access_to_additional_buckets',
                    policy_name='access_to_additional_buckets',
                    statements=[
                        aws_iam.PolicyStatement(
                            actions=['s3:GetObject*', 's3:GetBucket*',
                                     's3:List*'],
                            resources=sorted(set(buckets_list))
                        )
                    ]
                )
            )
        kms_aes_siem.grant_decrypt(lambda_es_loader)

        ######################################################################
        # s3 notification and grant permisssion
        ######################################################################
        s3_geo.grant_read_write(lambda_geo)
        s3_geo.grant_read(lambda_es_loader)
        s3_log.grant_read(lambda_es_loader)

        # create s3 notification for es_loader
        notification = aws_s3_notifications.LambdaDestination(lambda_es_loader)

        # assign notification for the s3 PUT event type
        # most log system use PUT, but also CLB use POST & Multipart Upload
        s3_log.add_event_notification(
            aws_s3.EventType.OBJECT_CREATED, notification,
            aws_s3.NotificationKeyFilter(prefix='AWSLogs/'))

        # For user logs, not AWS logs
        s3_log.add_event_notification(
            aws_s3.EventType.OBJECT_CREATED, notification,
            aws_s3.NotificationKeyFilter(prefix='UserLogs/'))

        # Download geoip to S3 once by executing lambda_geo
        get_geodb = aws_cloudformation.CfnCustomResource(
            self, 'ExecLambdaGeoipDownloader',
            service_token=lambda_geo.function_arn,
        )
        get_geodb.cfn_options.deletion_policy = core.CfnDeletionPolicy.RETAIN

        # Download geoip every day at 6PM UTC
        rule = aws_events.Rule(
            self, 'CwlRuleLambdaGeoipDownloaderDilly',
            schedule=aws_events.Schedule.cron(
                minute='20', hour='0', month='*', week_day='*', year='*'),
        )
        rule.add_target(aws_events_targets.LambdaFunction(lambda_geo))

        ######################################################################
        # bucket policy
        ######################################################################
        s3_awspath = s3_log.bucket_arn + '/AWSLogs/' + core.Aws.ACCOUNT_ID
        bucket_policy_common1 = aws_iam.PolicyStatement(
            sid='ELB Policy',
            principals=[aws_iam.AccountPrincipal(
                account_id=elb_accounts.find_in_map(
                    core.Aws.REGION, 'accountid'))],
            actions=['s3:PutObject'], resources=[s3_awspath + '/*'],)
        # NLB / ALB / R53resolver / VPC Flow Logs
        bucket_policy_elb1 = aws_iam.PolicyStatement(
            sid='AWSLogDeliveryAclCheck For ALB NLB R53Resolver Flowlogs',
            principals=[aws_iam.ServicePrincipal(
                'delivery.logs.amazonaws.com')],
            actions=['s3:GetBucketAcl', 's3:ListBucket'],
            resources=[s3_log.bucket_arn],)
        bucket_policy_elb2 = aws_iam.PolicyStatement(
            sid='AWSLogDeliveryWrite For ALB NLB R53Resolver Flowlogs',
            principals=[aws_iam.ServicePrincipal(
                'delivery.logs.amazonaws.com')],
            actions=['s3:PutObject'], resources=[s3_awspath + '/*'],
            conditions={
                'StringEquals': {'s3:x-amz-acl': 'bucket-owner-full-control'}})
        s3_log.add_to_resource_policy(bucket_policy_common1)
        s3_log.add_to_resource_policy(bucket_policy_elb1)
        s3_log.add_to_resource_policy(bucket_policy_elb2)

        # CloudTrail
        bucket_policy_trail1 = aws_iam.PolicyStatement(
            sid='AWSLogDeliveryAclCheck For Cloudtrail',
            principals=[aws_iam.ServicePrincipal('cloudtrail.amazonaws.com')],
            actions=['s3:GetBucketAcl'], resources=[s3_log.bucket_arn],)
        bucket_policy_trail2 = aws_iam.PolicyStatement(
            sid='AWSLogDeliveryWrite For CloudTrail',
            principals=[aws_iam.ServicePrincipal('cloudtrail.amazonaws.com')],
            actions=['s3:PutObject'], resources=[s3_awspath + '/*'],
            conditions={
                'StringEquals': {'s3:x-amz-acl': 'bucket-owner-full-control'}})
        s3_log.add_to_resource_policy(bucket_policy_trail1)
        s3_log.add_to_resource_policy(bucket_policy_trail2)

        # GuardDuty
        bucket_policy_gd1 = aws_iam.PolicyStatement(
            sid='Allow GuardDuty to use the getBucketLocation operation',
            principals=[aws_iam.ServicePrincipal('guardduty.amazonaws.com')],
            actions=['s3:GetBucketLocation'], resources=[s3_log.bucket_arn],)
        bucket_policy_gd2 = aws_iam.PolicyStatement(
            sid='Allow GuardDuty to upload objects to the bucket',
            principals=[aws_iam.ServicePrincipal('guardduty.amazonaws.com')],
            actions=['s3:PutObject'], resources=[s3_log.bucket_arn + '/*'],)
        bucket_policy_gd5 = aws_iam.PolicyStatement(
            sid='Deny non-HTTPS access', effect=aws_iam.Effect.DENY,
            actions=['s3:*'], resources=[s3_log.bucket_arn + '/*'],
            conditions={'Bool': {'aws:SecureTransport': 'false'}})
        bucket_policy_gd5.add_any_principal()
        s3_log.add_to_resource_policy(bucket_policy_gd1)
        s3_log.add_to_resource_policy(bucket_policy_gd2)
        s3_log.add_to_resource_policy(bucket_policy_gd5)

        # Config
        bucket_policy_config1 = aws_iam.PolicyStatement(
            sid='AWSConfig BucketPermissionsCheck and BucketExistenceCheck',
            principals=[aws_iam.ServicePrincipal('config.amazonaws.com')],
            actions=['s3:GetBucketAcl', 's3:ListBucket'],
            resources=[s3_log.bucket_arn],)
        bucket_policy_config2 = aws_iam.PolicyStatement(
            sid='AWSConfigBucketDelivery',
            principals=[aws_iam.ServicePrincipal('config.amazonaws.com')],
            actions=['s3:PutObject'], resources=[s3_awspath + '/Config/*'],
            conditions={
                'StringEquals': {'s3:x-amz-acl': 'bucket-owner-full-control'}})
        s3_log.add_to_resource_policy(bucket_policy_config1)
        s3_log.add_to_resource_policy(bucket_policy_config2)

        # geoip
        bucket_policy_geo1 = aws_iam.PolicyStatement(
            sid='Allow geoip downloader and es-loader to read/write',
            principals=[lambda_es_loader.role, lambda_geo.role],
            actions=['s3:PutObject', 's3:GetObject', 's3:DeleteObject'],
            resources=[s3_geo.bucket_arn + '/*'],)
        s3_geo.add_to_resource_policy(bucket_policy_geo1)

        # ES Snapshot
        bucket_policy_snapshot = aws_iam.PolicyStatement(
            sid='Allow ES to store snapshot',
            principals=[aes_siem_snapshot_role],
            actions=['s3:PutObject', 's3:GetObject', 's3:DeleteObject'],
            resources=[s3_snapshot.bucket_arn + '/*'],)
        s3_snapshot.add_to_resource_policy(bucket_policy_snapshot)

        ######################################################################
        # for multiaccount / organizaitons
        ######################################################################
        if org_id or no_org_ids:
            ##################################################################
            # KMS key policy for multiaccount / organizaitons
            ##################################################################
            # for CloudTrail
            cond_tail2 = self.make_resource_list(
                path='arn:aws:cloudtrail:*:', tail=':trail/*',
                keys=[org_mgmt_id, no_org_ids])
            key_policy_mul_trail2 = aws_iam.PolicyStatement(
                sid=('Allow CloudTrail to encrypt logs for multiaccounts'),
                actions=['kms:GenerateDataKey*'],
                principals=[aws_iam.ServicePrincipal(
                    'cloudtrail.amazonaws.com')],
                resources=['*'],
                conditions={'StringLike': {
                    'kms:EncryptionContext:aws:cloudtrail:arn': cond_tail2}})
            kms_aes_siem.add_to_resource_policy(key_policy_mul_trail2)

            # for replicaiton
            key_policy_rep1 = aws_iam.PolicyStatement(
                sid=('Enable cross account encrypt access for S3 Cross Region '
                     'Replication'),
                actions=['kms:Encrypt'],
                principals=self.make_account_plincipals(
                    org_mgmt_id, org_member_ids, no_org_ids),
                resources=['*'],)
            kms_aes_siem.add_to_resource_policy(key_policy_rep1)

            ##################################################################
            # Buckdet Policy for multiaccount / organizaitons
            ##################################################################
            s3_log_bucket_arn = 'arn:aws:s3:::' + s3bucket_name_log

            # for CloudTrail
            s3_mulpaths = self.make_resource_list(
                path=f'{s3_log_bucket_arn}/AWSLogs/', tail='/*',
                keys=[org_id, org_mgmt_id, no_org_ids])
            bucket_policy_org_trail = aws_iam.PolicyStatement(
                sid='AWSCloudTrailWrite for Multiaccounts / Organizations',
                principals=[
                    aws_iam.ServicePrincipal('cloudtrail.amazonaws.com')],
                actions=['s3:PutObject'], resources=s3_mulpaths,
                conditions={'StringEquals': {
                    's3:x-amz-acl': 'bucket-owner-full-control'}})
            s3_log.add_to_resource_policy(bucket_policy_org_trail)

            # config
            s3_conf_multpaths = self.make_resource_list(
                path=f'{s3_log_bucket_arn}/AWSLogs/', tail='/Config/*',
                keys=[org_id, org_mgmt_id, no_org_ids])
            bucket_policy_mul_config2 = aws_iam.PolicyStatement(
                sid='AWSConfigBucketDelivery',
                principals=[aws_iam.ServicePrincipal('config.amazonaws.com')],
                actions=['s3:PutObject'], resources=s3_conf_multpaths,
                conditions={'StringEquals': {
                    's3:x-amz-acl': 'bucket-owner-full-control'}})
            s3_log.add_to_resource_policy(bucket_policy_mul_config2)

            # for replication
            bucket_policy_rep1 = aws_iam.PolicyStatement(
                sid='PolicyForDestinationBucket / Permissions on objects',
                principals=self.make_account_plincipals(
                    org_mgmt_id, org_member_ids, no_org_ids),
                actions=['s3:ReplicateDelete', 's3:ReplicateObject',
                         's3:ReplicateTags', 's3:GetObjectVersionTagging',
                         's3:ObjectOwnerOverrideToBucketOwner'],
                resources=[f'{s3_log_bucket_arn}/*'])
            bucket_policy_rep2 = aws_iam.PolicyStatement(
                sid='PolicyForDestinationBucket / Permissions on bucket',
                principals=self.make_account_plincipals(
                    org_mgmt_id, org_member_ids, no_org_ids),
                actions=['s3:List*', 's3:GetBucketVersioning',
                         's3:PutBucketVersioning'],
                resources=[f'{s3_log_bucket_arn}'])
            s3_log.add_to_resource_policy(bucket_policy_rep1)
            s3_log.add_to_resource_policy(bucket_policy_rep2)

        ######################################################################
        # SNS topic for Amazon ES Alert
        ######################################################################
        sns_topic = aws_sns.Topic(
            self, 'SnsTopic', topic_name='aes-siem-alert',
            display_name='AES SIEM')

        sns_topic.add_subscription(aws_sns_subscriptions.EmailSubscription(
            email_address=sns_email.value_as_string))
        sns_topic.grant_publish(aes_siem_sns_role)

        ######################################################################
        # output of CFn
        ######################################################################
        kibanaurl = f'https://{es_endpoint}/_plugin/kibana/'
        kibanaadmin = aes_domain.get_att('kibanaadmin').to_string()
        kibanapass = aes_domain.get_att('kibanapass').to_string()

        core.CfnOutput(self, 'RoleDeploy', export_name='role-deploy',
                       value=aes_siem_deploy_role_for_lambda.role_arn)
        core.CfnOutput(self, 'KibanaUrl', export_name='kibana-url',
                       value=kibanaurl)
        core.CfnOutput(self, 'KibanaPassword', export_name='kibana-pass',
                       value=kibanapass,
                       description='Please change the password in Kibana ASAP')
        core.CfnOutput(self, 'KibanaAdmin', export_name='kibana-admin',
                       value=kibanaadmin)

    def make_account_plincipals(self, *args):
        aws_ids = []
        for arg in args:
            if isinstance(arg, str):
                aws_ids.append(arg)
            elif isinstance(arg, list):
                aws_ids.extend(arg)
        account_plincipals = []
        for aws_id in sorted(set(aws_ids)):
            account_plincipals.append(
                aws_iam.AccountPrincipal(account_id=aws_id))
        return account_plincipals

    def make_resource_list(self, path=None, tail=None, keys=[]):
        aws_ids = []
        for key in keys:
            if isinstance(key, str):
                aws_ids.append(key)
            elif isinstance(key, list):
                aws_ids.extend(key)
        multi_s3path = []
        for aws_id in sorted(set(aws_ids)):
            multi_s3path.append(path + aws_id + tail)
        return multi_s3path
