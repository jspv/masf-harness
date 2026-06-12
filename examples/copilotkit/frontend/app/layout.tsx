import type { ReactNode } from "react";
import { CopilotKit } from "@copilotkit/react-core";
import "@copilotkit/react-ui/styles.css";

export const metadata = {
  title: "harness × CopilotKit",
  description: "CopilotKit frontend for the harness AG-UI backend",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        {/* runtimeUrl points at the route in app/api/copilotkit; `agent` matches the
            key registered in CopilotRuntime's `agents` map. */}
        <CopilotKit runtimeUrl="/api/copilotkit" agent="harness">
          {children}
        </CopilotKit>
      </body>
    </html>
  );
}
