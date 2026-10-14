# V6 verification

Parent reran 44 provisioning tests and 103 discovery tests: all 147 passed.

Fresh isolated Docker installation via `bash setup.sh --self-test` exited 0. Verified complete RFB/frame transport through authenticated TLS Caddy WebSocket, loopback-only HTTPS, no Docker socket, private backend ports and custom loopback HTTPS profile. Test containers stopped. No live VPS changes, no model/provider login, no private lead fixtures.

Host tested: Linux ARM64. Windows/WSL2, Intel/AMD PC and macOS instructions are provided, but those machines have not been exercised here. A new real ten-contact acceptance test of v6 remains unverified.
