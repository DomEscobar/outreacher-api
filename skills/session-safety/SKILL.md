---
name: session-safety
description: Use when controlling this browser session. Human login and bounded authorized actions.
---
# Session-Sicherheit

Der Remote-Desktop und Browserzustand gehören dem Nutzer. Der Nutzer tippt Logins selbst. Während Login und 2FA pausieren Agent und Aufzeichnung. Keine Cookie-/Credential-Extraktion und keine Bildschirmaufzeichnungen. Öffentliche URLs dürfen keine CDP-, Docker-, VNC- oder interne Adminports anbieten. Keine Nachrichten, Kontaktanfragen, Käufe oder andere externe Mutationen ohne gesonderte Autorisierung. Browserprofile enthalten sensitive Login-Sessions und sind wie Secrets zu schützen. Default: menschlicher Review, keine autonome LinkedIn-Discovery. Skills sind Verhaltensregeln, keine technische Vollsperre.
