# Azure Email SMTP in BB Studio

Use this connection for mail to recipients who have agreed to receive it.

## Set up the Azure sender

1. In Azure, provision and verify a domain in **Email Communication Services**,
   and link it to an **Azure Communication Services** resource. Copy the
   provisioned **MailFrom** address from the email domain.
   [Connect a verified domain](https://learn.microsoft.com/azure/communication-services/quickstarts/email/connect-email-communication-resource)
2. Associate an SMTP username with a Microsoft Entra application that has
   permission to submit mail through that resource. Use the SMTP username
   and an active Entra client secret value.
   [Set up Azure SMTP authentication](https://learn.microsoft.com/azure/communication-services/quickstarts/email/send-email-smtp/smtp-authentication)
3. In BB, go to **Sending providers → Configure Azure Email**. Enter the
   MailFrom address, SMTP username, and client secret. BB tests authentication
   at `smtp.azurecomm.net:587` over STARTTLS before saving the connection.
   Azure checks the sender address on submission.

## Review a message

Choose the Azure account in Studio's **From** menu. Set the **Subject** and,
if needed, **Reply-To** to a reply inbox you control. Select **Create one
draft per To address** for separate reviews and deliveries. Studio provides
plain text, HTML, images, attachments, and previews. You can review each
draft or approve drafts together in the review queue.

Use a provisioned Azure MailFrom identity for each From option. Set its
display name on the sender resource in Azure.
[Manage Azure sender addresses](https://learn.microsoft.com/azure/communication-services/quickstarts/email/add-multiple-senders)

Azure applies subscription quotas and limits the complete message to 10 MB
and 50 recipients. BB allows up to 7 MB of raw attachments for Azure to
leave room for MIME encoding. Check the subscription's quota before
scheduling mail.
[Azure Email service limits](https://learn.microsoft.com/azure/communication-services/concepts/service-limits#email)

A successful BB send means Azure's SMTP server accepted the message. To
verify inbox delivery, send a single approved test to an inbox you control
and check the From address, Subject, and Reply-To there. Azure delivery
tracking is available in Azure Monitor and Event Grid.
[Azure Email delivery reports](https://learn.microsoft.com/azure/event-grid/communication-services-email-events)
