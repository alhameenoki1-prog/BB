-- Isolated local test sink. Never deployed for internet mail delivery.
-- Port is bound to 127.0.0.1:2025 by the evaluation workflow.
local kumo = require 'kumo'

kumo.on('init', function()
  kumo.start_esmtp_listener {
    listen = '0:25',
    relay_hosts = { '0.0.0.0/0' },
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
