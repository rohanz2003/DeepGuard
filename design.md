# UI/UX and Visual Design

## 1. UI/UX Principles
- Build a clean, analytical interface suitable for security monitoring.
- Use a mobile-first responsive layout.
- Reuse cards and status components across uploaded-audio scans and any supported call-monitoring views.
- Include accessible labels, keyboard navigation, clear focus states, and readable contrast.
- Make empty, loading, success, and error states explicit.

## 2. Color and Theme
- **Primary:** Deep blue and professional slate tones.
- **Safe/Lower Risk:** Green `#28a745`.
- **Review/Suspicious:** Amber `#ffc107`.
- **High Risk:** Red `#dc3545`.
- Include a neutral state for unavailable or inconclusive results.
- Support light and dark themes, and do not rely on color alone to communicate status.

## 3. Typography
- Use Inter or Roboto, with a system-font fallback.
- Create a clear hierarchy for page titles, section headings, body text, and metadata.
- Display prediction scores with context and caveats, not as definitive truth.

## 4. Dashboard Components
- Navigation/sidebar
- Total scans and result-summary cards
- Audio upload and scan status
- Result summary with confidence/risk explanation
- Scan history table/list
- Model version and analysis timestamp
- Privacy and audio-retention controls

## 5. Remembered UI Preferences
- Remember the preferred dashboard tab (Live Calls only if the feature is configured).
- Persist light/dark theme preference.
- Persist sidebar collapsed/expanded state.
- Store only non-sensitive UI preferences in local storage.
