-- Isolated local test sink. Never deployed for internet mail delivery.
-- Port is bound to 127.0.0.1:2025 by the evaluation workflow.
local kumo = require 'kumo'

kumo.on('init', function()
  kumo.start_esmtp_listener {
    listen = '0:25',
    relay_hosts = { '0.0.0.0/0' },
  }

  -- HTTP injection test: mapped to host loopback only in GitHub Actions.
  -- Trust Docker's local bridge network; never deploy this policy publicly.
  kumo.start_http_listener {
    listen = '0.0.0.0:8005',
    trusted_hosts = { '127.0.0.1', '::1', '172.16.0.0/12' },
  }

  kumo.define_spool {
    name = 'data',
    path = '/tmp/kumo-sink/data',
  }
  kumo.define_spool {
    name = 'meta',
    path = '/tmp/kumo-sink/meta',
  }
end)

kumo.on('smtp_server_message_received', function(msg)
  msg:set_meta('queue', 'null')
end)

-- SMTP and HTTP submissions both use a null queue, never a public MX.
kumo.on('http_message_generated', function(msg)
  msg:set_meta('queue', 'null')
end)
