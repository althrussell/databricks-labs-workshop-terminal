# AppKit compatibility without forced Databricks styling

When AppKit is detected:

- Load `databricks-apps` for project structure, plugins, authentication, and deployment.
- Use AppKit primitives where they solve the required behaviour.
- Verify component exports against the installed AppKit version.
- Apply the selected brand through supported theme variables, semantic tokens, layout, typography, imagery, and application CSS.
- Do not copy private console tokens or duplicate workspace chrome.
- Platform-native visual alignment is one valid mode, not the default.
- A branded customer website running as a Databricks App should still look like the customer brand.
