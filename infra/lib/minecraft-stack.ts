import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as s3assets from 'aws-cdk-lib/aws-s3-assets';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { Category, MinecraftWorld } from './manifests';

const SSM_PREFIX = '/crossroads-mc';

export interface MinecraftStackProps extends cdk.StackProps {
  sshCidr?: string;
  /** Apex domain owning the Route 53 hosted zone (from infra/config.json). */
  domainName: string;
  /** EC2 instance type for the game server (from infra/config.json). */
  instanceType: string;
  /** Persistent EBS data volume size in GiB (from infra/config.json). */
  ebsSize: number;
  /** Categories scanned from infra/manifests/categories/ (validated in bin). */
  categories: Category[];
  /** Worlds scanned from infra/manifests/worlds/ (validated in bin). */
  worlds: MinecraftWorld[];
}

export class MinecraftStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: MinecraftStackProps) {
    super(scope, id, props);

    const sshCidr = props.sshCidr ?? '0.0.0.0/0';

    // Combined runtime manifest injected to the instance + Lambda.
    const manifestJson = JSON.stringify({
      categories: props.categories,
      worlds: props.worlds,
    });

    cdk.Tags.of(this).add('Project', 'CrossroadsMC');
    cdk.Tags.of(this).add('Environment', 'Production');

    // ── VPC: single public subnet, no NAT gateway ──────────────────────────
    const vpc = new ec2.Vpc(this, 'McVpc', {
      maxAzs: 1,
      subnetConfiguration: [
        {
          name: 'Public',
          subnetType: ec2.SubnetType.PUBLIC,
          cidrMask: 24,
        },
      ],
      natGateways: 0,
    });

    // ── Security Group ──────────────────────────────────────────────────────
    const sg = new ec2.SecurityGroup(this, 'McSG', {
      vpc,
      description: 'Minecraft server',
      allowAllOutbound: true,
    });
    // One public game port per category slot — discovered from the manifests.
    for (const cat of props.categories) {
      sg.addIngressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(cat.port), `Minecraft - ${cat.name} slot`);
    }
    sg.addIngressRule(ec2.Peer.ipv4(sshCidr), ec2.Port.tcp(22), 'SSH');
    sg.addIngressRule(ec2.Peer.ipv4(sshCidr), ec2.Port.tcp(25575), 'Restricted RCON Access');

    // ── Elastic IP ──────────────────────────────────────────────────────────
    const eip = new ec2.CfnEIP(this, 'McEip', {
      tags: [{ key: 'Name', value: 'minecraft-eip' }],
    });

    // ── Route 53 DNS ──────────────────────────────────────────────────────────
    // CDK owns the public hosted zone; repoint the registrar NS to its
    // nameservers (see the HostedZoneNameServers output).
    const zone = new route53.PublicHostedZone(this, 'McZone', {
      zoneName: props.domainName,
    });
    // A record: server.<domain> → Elastic IP.
    new route53.ARecord(this, 'ServerA', {
      zone,
      recordName: 'server',
      target: route53.RecordTarget.fromIpAddresses(eip.ref),
    });
    // One SRV record per category so clients connect on the standard MC port:
    // _minecraft._tcp.<category>.<domain> → 0 5 <port> server.<domain>.
    for (const cat of props.categories) {
      new route53.SrvRecord(this, `Srv${cat.name}`, {
        zone,
        recordName: `_minecraft._tcp.${cat.name}`,
        values: [{ priority: 0, weight: 5, port: cat.port, hostName: `server.${props.domainName}` }],
      });
    }

    // ── Persistent EBS Data Volume (RETAIN on destroy) ──────────────────────
    // Holds ALL world cartridges — there is no S3 tiering; worlds never leave EBS.
    const dataVolume = new ec2.Volume(this, 'McDataVolume', {
      availabilityZone: vpc.availabilityZones[0],
      size: cdk.Size.gibibytes(props.ebsSize),
      volumeType: ec2.EbsDeviceVolumeType.GP3,
      throughput: 125,
      iops: 3000,
      encrypted: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // ── IAM Role ────────────────────────────────────────────────────────────
    const role = new iam.Role(this, 'McRole', {
      assumedBy: new iam.ServicePrincipal('ec2.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('AmazonSSMManagedInstanceCore'),
      ],
    });
    role.addToPolicy(new iam.PolicyStatement({
      actions: ['ec2:AssociateAddress', 'ec2:DescribeAddresses'],
      resources: ['*'],
    }));
    // Read the asset locations + read/seed the live-category pointer
    // (mc-boot.sh, start-server.sh, mc-swap.sh).
    role.addToPolicy(new iam.PolicyStatement({
      actions: ['ssm:GetParameter', 'ssm:GetParameters', 'ssm:PutParameter'],
      resources: [`arn:aws:ssm:${this.region}:${this.account}:parameter${SSM_PREFIX}/*`],
    }));
    // Decrypt the SecureString RCON password (encrypted with the aws/ssm managed
    // key) when provision-worlds.sh reads it. Scoped to SSM-mediated decrypts.
    role.addToPolicy(new iam.PolicyStatement({
      actions: ['kms:Decrypt'],
      resources: ['*'],
      conditions: { StringEquals: { 'kms:ViaService': `ssm.${this.region}.amazonaws.com` } },
    }));
    // Publish the disk-usage custom metric (PutMetricData has no resource scoping).
    role.addToPolicy(new iam.PolicyStatement({
      actions: ['cloudwatch:PutMetricData'],
      resources: ['*'],
    }));

    // ── CDK Assets (config + scripts + manifest bundled to S3 on cdk deploy) ──
    const configAsset = new s3assets.Asset(this, 'ConfigAsset', {
      path: path.join(__dirname, '..', '..', 'config'),
    });
    const scriptsAsset = new s3assets.Asset(this, 'ScriptsAsset', {
      path: path.join(__dirname, '..', '..', 'scripts'),
    });
    // The combined manifest is generated at synth into a temp file and uploaded as
    // a standalone (un-zipped) asset, so the instance can `aws s3 cp` it directly.
    const manifestFile = path.join(os.tmpdir(), `crossroads-manifest-${id}.json`);
    fs.writeFileSync(manifestFile, manifestJson);
    const manifestAsset = new s3assets.Asset(this, 'ManifestAsset', { path: manifestFile });

    configAsset.grantRead(role);
    scriptsAsset.grantRead(role);
    manifestAsset.grantRead(role);

    // Asset S3 URLs change on every deploy (content-hashed keys). Publish them to
    // SSM so a stopped instance can always fetch the latest on its next boot
    // (mc-boot.sh / mc-swap.sh re-sync from these before starting a slot).
    const assetParams = {
      scripts: new ssm.StringParameter(this, 'ScriptsAssetParam', {
        parameterName: `${SSM_PREFIX}/asset/scripts`,
        stringValue: scriptsAsset.s3ObjectUrl,
      }),
      config: new ssm.StringParameter(this, 'ConfigAssetParam', {
        parameterName: `${SSM_PREFIX}/asset/config`,
        stringValue: configAsset.s3ObjectUrl,
      }),
      manifest: new ssm.StringParameter(this, 'ManifestAssetParam', {
        parameterName: `${SSM_PREFIX}/asset/manifest`,
        stringValue: manifestAsset.s3ObjectUrl,
      }),
    };
    Object.values(assetParams).forEach((p) => p.grantRead(role));

    // ── EC2 Instance ────────────────────────────────────────────────────────
    const instance = new ec2.Instance(this, 'McServer', {
      vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
      instanceType: new ec2.InstanceType(props.instanceType),
      // Ubuntu 24.04 LTS (arm64/Graviton) — resolves to latest AMI at deploy time
      machineImage: ec2.MachineImage.fromSsmParameter(
        '/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id',
        { os: ec2.OperatingSystemType.LINUX },
      ),
      securityGroup: sg,
      role,
      // Slim root volume; game data lives on the separate EBS
      blockDevices: [
        {
          deviceName: '/dev/sda1',
          volume: ec2.BlockDeviceVolume.ebs(10, {
            volumeType: ec2.EbsDeviceVolumeType.GP3,
          }),
        },
      ],
      requireImdsv2: true,
      // User-data runs only once per instance, so a user-data edit must replace
      // the instance to take effect. The data volume is RETAIN, so no data loss.
      userDataCausesReplacement: true,
    });

    // mc-boot reads the asset-location params on first boot, so make sure they
    // exist before the instance launches.
    Object.values(assetParams).forEach((p) => instance.node.addDependency(p));

    // ── Attach persistent data volume ────────────────────────────────────────
    new ec2.CfnVolumeAttachment(this, 'McDataAttachment', {
      instanceId: instance.instanceId,
      volumeId: dataVolume.volumeId,
      device: '/dev/sdh',
    });

    // ── User Data ────────────────────────────────────────────────────────────
    instance.addUserData(...buildUserData({
      eipAllocationId: eip.attrAllocationId,
      region: this.region,
    }));

    // ── EIP association (CloudFormation manages on initial deploy; ───────────
    // ── user data re-associates on instance replacement)            ──────────
    new ec2.CfnEIPAssociation(this, 'McEipAssoc', {
      instanceId: instance.instanceId,
      allocationId: eip.attrAllocationId,
    });

    // ── CloudWatch alarm: low network → server is idle ───────────────────────
    // 4 × 5-minute periods = 20 minutes below threshold before alarm fires
    new cloudwatch.Alarm(this, 'McLowNetworkAlarm', {
      alarmName: 'minecraft-idle',
      alarmDescription: 'Minecraft server idle — candidate for auto-shutdown',
      metric: new cloudwatch.Metric({
        namespace: 'AWS/EC2',
        metricName: 'NetworkPacketsIn',
        dimensionsMap: { InstanceId: instance.instanceId },
        period: cdk.Duration.minutes(5),
        statistic: 'Sum',
      }),
      threshold: 100,
      evaluationPeriods: 4,
      comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // ── CloudWatch alarm: EBS data volume filling up ─────────────────────────
    // Fed by scripts/disk-check.sh (cron) which publishes the custom metric.
    new cloudwatch.Alarm(this, 'McDiskUsageAlarm', {
      alarmName: 'minecraft-disk-usage',
      alarmDescription: 'Minecraft EBS data volume is running low on free space',
      metric: new cloudwatch.Metric({
        namespace: 'CrossroadsMC',
        metricName: 'DiskUsedPercent',
        dimensionsMap: { InstanceId: instance.instanceId },
        period: cdk.Duration.minutes(5),
        statistic: 'Maximum',
      }),
      threshold: 85,
      evaluationPeriods: 2,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // ── Discord Bot Lambda ────────────────────────────────────────────────────
    const discordBotRole = new iam.Role(this, 'DiscordBotRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole'),
      ],
    });
    // DescribeInstances does not support resource-level permissions
    discordBotRole.addToPolicy(new iam.PolicyStatement({
      actions: ['ec2:DescribeInstances'],
      resources: ['*'],
    }));
    // StartInstances / StopInstances scoped to this specific instance
    discordBotRole.addToPolicy(new iam.PolicyStatement({
      actions: ['ec2:StartInstances', 'ec2:StopInstances'],
      resources: [
        `arn:aws:ec2:${this.region}:${this.account}:instance/${instance.instanceId}`,
      ],
    }));
    // Hot-swap: run mc-swap.sh on the instance via SSM Run Command.
    discordBotRole.addToPolicy(new iam.PolicyStatement({
      actions: ['ssm:SendCommand'],
      resources: [
        `arn:aws:ec2:${this.region}:${this.account}:instance/${instance.instanceId}`,
        `arn:aws:ssm:${this.region}::document/AWS-RunShellScript`,
      ],
    }));
    // Read/seed the live-category pointer.
    discordBotRole.addToPolicy(new iam.PolicyStatement({
      actions: ['ssm:GetParameter', 'ssm:PutParameter'],
      resources: [`arn:aws:ssm:${this.region}:${this.account}:parameter${SSM_PREFIX}/*`],
    }));
    // Self-invoke: the bot ACKs Discord with a deferred response, then async-invokes
    // itself to finish the slow EC2/SSM work and edit the message. Referenced by a
    // fixed function name (not the function token) to avoid a role↔function cycle.
    const discordFnName = 'crossroads-mc-discord-bot';
    discordBotRole.addToPolicy(new iam.PolicyStatement({
      actions: ['lambda:InvokeFunction'],
      resources: [`arn:aws:lambda:${this.region}:${this.account}:function:${discordFnName}`],
    }));

    const discordFn = new lambda.DockerImageFunction(this, 'DiscordBot', {
      // Built from discord-bot/Dockerfile and pushed to ECR on cdk deploy.
      functionName: discordFnName,
      code: lambda.DockerImageCode.fromImageAsset(path.join(__dirname, '..', '..', 'discord-bot')),
      role: discordBotRole,
      // Phase-2 worker may retry the @original edit with backoff (up to ~6.5s)
      // while the deferred ACK propagates, on top of a cold start + EC2/SSM work.
      timeout: cdk.Duration.seconds(20),
      // Lambda scales CPU with memory. At the 128 MB default a container cold
      // start takes ~2.4s of init, which pushes the Phase-1 deferred ACK past
      // Discord's hard 3s interaction deadline (the interaction is then dropped
      // and every followup 404s "Unknown Webhook"). This bot needs the CPU, not
      // the RAM (max used ~112 MB), purely to cold-start fast enough.
      memorySize: 1024,
      environment: {
        INSTANCE_ID: instance.instanceId,
        // Discord public key + admin role id are read from SSM at runtime
        // (/crossroads-mc/discord/*), not injected here.
        // Apex domain; the bot builds per-category connect hosts as
        // <category>.<domain> (matching the SRV records).
        DOMAIN_NAME: props.domainName,
        SSM_PREFIX,
        MANIFEST_JSON: manifestJson,
      },
    });

    const discordFnUrl = discordFn.addFunctionUrl({
      authType: lambda.FunctionUrlAuthType.NONE,
    });

    // ── Outputs ──────────────────────────────────────────────────────────────
    new cdk.CfnOutput(this, 'ServerIP', {
      value: eip.ref,
      description: 'Minecraft server public IP',
    });
    new cdk.CfnOutput(this, 'InstanceId', {
      value: instance.instanceId,
      description: 'EC2 instance ID',
    });
    new cdk.CfnOutput(this, 'DataVolumeId', {
      value: dataVolume.volumeId,
      description: 'Persistent EBS data volume ID',
    });
    new cdk.CfnOutput(this, 'DiscordWebhookUrl', {
      value: discordFnUrl.url,
      description: 'Paste into Discord Developer Portal → Interactions Endpoint URL',
    });
    new cdk.CfnOutput(this, 'HostedZoneNameServers', {
      value: cdk.Fn.join(', ', zone.hostedZoneNameServers ?? []),
      description: 'Set these as the domain nameservers at your registrar',
    });
  }
}

// ── User Data builder ─────────────────────────────────────────────────────────

interface UserDataParams {
  eipAllocationId: string;
  region: string;
}

function buildUserData(p: UserDataParams): string[] {
  return [
    // Pipe all output to both the journal and a file for easy debugging
    'exec > >(tee /var/log/user-data.log | logger -t user-data -s 2>/dev/console) 2>&1',
    'set -euo pipefail',

    // ── 1. Dependencies (both JDKs: 1.20.1 → 17, 1.20.5+ → 21) ───────────────
    // NB: Ubuntu 24.04 (noble) dropped the `awscli` apt package, so install the
    // AWS CLI v2 from the official bundle instead (lands in /usr/local/bin, which
    // is on systemd's default PATH for the scripts that call `aws` later).
    'apt-get update -y',
    'apt-get install -y openjdk-17-jre-headless openjdk-21-jre-headless jq unzip wget curl',
    'curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip" -o /tmp/awscliv2.zip',
    'unzip -q /tmp/awscliv2.zip -d /tmp',
    '/tmp/aws/install --update',
    'rm -rf /tmp/aws /tmp/awscliv2.zip',

    // ── 2. EIP association ───────────────────────────────────────────────────
    `REGION="${p.region}"`,
    `EIP_ALLOC="${p.eipAllocationId}"`,
    'TOKEN=$(curl -sf -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")',
    'INSTANCE_ID=$(curl -sf -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id)',
    'aws ec2 associate-address --instance-id "$INSTANCE_ID" --allocation-id "$EIP_ALLOC" --region "$REGION" || true',

    // ── 3. EBS mount ─────────────────────────────────────────────────────────
    'MOUNT_POINT=/mnt/minecraft-data',
    'mkdir -p "$MOUNT_POINT"',

    // The attached volume may take a moment to appear after instance start
    'set +e',
    'for i in $(seq 1 30); do',
    '  ROOT_PART=$(findmnt -no SOURCE / 2>/dev/null)',
    '  ROOT_DISK=$(lsblk -no PKNAME "$ROOT_PART" 2>/dev/null)',
    // Filter to TYPE=="disk" so snapd loop devices (loop0, loop1, …) — which sort
    // before nvme1n1 in lsblk output — can never be picked as the data volume.
    '  DATA_DISK=$(lsblk -dno NAME,TYPE 2>/dev/null | awk \'$2=="disk"{print $1}\' | grep -v "^${ROOT_DISK:-nomatch}$" | head -1)',
    '  [ -n "$DATA_DISK" ] && break',
    '  sleep 2',
    'done',
    'set -e',
    'if [ -z "$DATA_DISK" ]; then',
    '  echo "CRITICAL: EBS Data Volume failed to attach within 60s. Aborting." >&2',
    '  exit 1',
    'fi',
    'DEVICE="/dev/$DATA_DISK"',

    // Format only on first boot (raw volume has no filesystem signature)
    'if ! blkid "$DEVICE" | grep -q ext4; then',
    '  mkfs.ext4 -F "$DEVICE"',
    'fi',

    'mount "$DEVICE" "$MOUNT_POINT"',
    'DEVICE_UUID=$(blkid -s UUID -o value "$DEVICE")',
    'grep -q "$DEVICE_UUID" /etc/fstab || echo "UUID=$DEVICE_UUID $MOUNT_POINT ext4 defaults,nofail 0 2" >> /etc/fstab',

    'ln -sfn "$MOUNT_POINT" /home/ubuntu/minecraft',
    'chown -h ubuntu:ubuntu /home/ubuntu/minecraft',

    // ── 4. Bootstrap scripts from S3 ─────────────────────────────────────────
    // Just enough to run mc-boot; mc-boot.sh re-syncs scripts/config/manifest
    // from the SSM-published asset locations on every boot.
    //
    // The scripts URL is read from SSM rather than embedded here on purpose: the
    // asset key is content-hashed and changes on every script/config/manifest
    // edit. Embedding it would churn the user-data text and — with
    // userDataCausesReplacement — force a full instance replacement for every
    // script change, defeating the sync_assets re-sync model. Reading it from SSM
    // keeps user-data stable, so only genuine user-data logic changes replace.
    `SCRIPTS_URL=$(aws ssm get-parameter --name "${SSM_PREFIX}/asset/scripts" --query Parameter.Value --output text --region "$REGION")`,
    'aws s3 cp "$SCRIPTS_URL" /tmp/scripts.zip --region "$REGION"',
    'mkdir -p "$MOUNT_POINT/scripts"',
    'unzip -o /tmp/scripts.zip -d "$MOUNT_POINT/scripts/"',
    'chmod +x "$MOUNT_POINT/scripts/"*.sh',

    // ── 5. systemd units ─────────────────────────────────────────────────────
    'cp "$MOUNT_POINT/scripts/minecraft@.service" /etc/systemd/system/minecraft@.service',
    'cp "$MOUNT_POINT/scripts/mc-boot.service" /etc/systemd/system/mc-boot.service',
    'systemctl daemon-reload',

    // ── 6. Boot the live slot, and re-arm it for every future boot ───────────
    // mc-boot re-syncs assets from S3, seeds the live-category pointer, then
    // starts the live slot.
    'systemctl enable mc-boot.service',
    'systemctl start mc-boot.service',

    // ── 7. Cron jobs: idle auto-shutdown + disk-usage metric ─────────────────
    'echo "*/20 * * * * root /mnt/minecraft-data/scripts/heartbeat.sh >> /var/log/mc-heartbeat.log 2>&1" > /etc/cron.d/mc-heartbeat',
    'echo "*/5 * * * * root /mnt/minecraft-data/scripts/disk-check.sh >> /var/log/mc-disk.log 2>&1" > /etc/cron.d/mc-disk',
  ];
}
