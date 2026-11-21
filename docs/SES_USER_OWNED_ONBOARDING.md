# Connect Amazon SES to BB Studio

## Configure the BB installation

1. Install the optional AWS SDK with `python -m pip install -e '.[ses]'`.
2. Run BB with AWS workload credentials or a local AWS profile for a role that can assume the customer role. Set `BB_SES_PRINCIPAL_ARN` to the IAM principal ARN shown in the role's trust policy. Restart the BB web app and worker after configuring the installation.

## Connect an owned AWS account

1. In the intended AWS region, verify the sender address or its domain under **Verified identities** in Amazon SES. For domains, publish the DKIM records that SES gives you.
2. In **BB → Sender accounts → Amazon SES**, open the workspace's trust policy. Use it for a new IAM role in the account that owns the SES identity. The role grants `ses:GetAccount`, `ses:GetEmailIdentity`, and `ses:SendEmail`. Scope sending to the verified SES identity ARN and permitted From addresses. The BB role assumes it with temporary AWS credentials.
3. Enter the AWS account ID, region, role ARN and verified From address in BB. **Verify and connect** checks the role, identity and current regional quota. Select the Amazon SES sender in Studio and save a draft for review.
4. For real recipients, request production access in the SES console and wait for AWS approval. The console shows quota and the available options for requesting an increase. The quota counts recipients over a rolling 24-hour window. BB shows the approved limit and the remaining quota AWS reports.

## Track delivery

1. Create an SES configuration set that publishes **Delivery**, **Bounce**, **Complaint**, **Reject** and, if needed, **Delivery delay** to an SNS topic in the same region.
2. Subscribe a dedicated standard SQS queue to the SNS topic. Limit its `sqs:SendMessage` policy to that topic. Configure a dead-letter queue for messages requiring separate handling. Grant the customer role `sqs:GetQueueAttributes`, `sqs:ReceiveMessage` and `sqs:DeleteMessage` on the feedback queue ARN.
3. Add the configuration set name and queue URL in BB. The worker matches events to SES message IDs it recorded, saves the events, and acknowledges each matching event in SQS. Permanent bounces and complaints suppress further sends to the affected addresses.
4. Use the AWS mailbox simulator for a controlled delivery test. In **Jobs**, provider acceptance means the provider accepted the request. In **Amazon SES → Delivery feedback**, a Delivery event means the recipient's server accepted it.

AWS references

- [Verified identities](https://docs.aws.amazon.com/ses/latest/dg/creating-identities.html)
- [Production access](https://docs.aws.amazon.com/ses/latest/dg/request-production-access.html)
- [Sending quotas](https://docs.aws.amazon.com/ses/latest/dg/manage-sending-quotas.html)
- [SES events and their fields](https://docs.aws.amazon.com/ses/latest/dg/event-publishing-retrieving-sns-contents.html)
