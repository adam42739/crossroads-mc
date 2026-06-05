#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { MinecraftStack } from '../lib/minecraft-stack';
import { loadConfig, loadManifest, validateManifest } from '../lib/manifests';

const app = new cdk.App();

// Global settings (domain, instance type, EBS size); fails synth if malformed.
const config = loadConfig();

// Scan infra/manifests/{categories,worlds} and fail synth on any malformed entry.
const manifest = loadManifest();
validateManifest(manifest);

new MinecraftStack(app, 'CrossroadsMcStack', {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: 'us-west-2',
  },
  // Pass your IP: cdk deploy --context sshCidr=1.2.3.4/32
  sshCidr: app.node.tryGetContext('sshCidr') ?? '0.0.0.0/0',
  domainName: config.domain_name,
  instanceType: config.instance_type,
  ebsSize: config.ebs_size,
  categories: manifest.categories,
  worlds: manifest.worlds,
});
