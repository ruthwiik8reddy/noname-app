# Portal, warranties, reporting and security development checkpoint

This feature-branch checkpoint contains ongoing work, not a production release.

Implemented paths include customer invitations and login, owned vehicle/job/estimate records, explicitly shared photos, manager-issued warranty terms, customer claims and manager responses. New reporting includes recorded labor, reviewed service contribution and a threshold-gated moving-average scenario. Security changes isolate customer sessions, protect media reads, retire old tracking links, require safe production configuration and disable implicit demo seeding.

Pre-push access-boundary checks cover customer isolation, invitation replay/expiry/revocation, warranty ownership and stale claim responses, private-media visibility and path normalization, safe redirects and production configuration. These checks found and fixed an audit SQL placeholder error and a normalized static-path media bypass.

Still required before release: comprehensive upload-content validation, remaining legacy route authorization/CSRF review, login throttling and account recovery, deployment-specific private-file migration, backup/restore automation, monitoring, credential rotation, forecast accuracy tests on representative data, and end-to-end browser validation of the new portal. No production readiness, independently verified warranty eligibility, customer ROI or trained forecasting is claimed.

All previously completed milestones remain on this feature branch. No changes are intended for main until review.
