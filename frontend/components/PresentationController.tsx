// SPDX-License-Identifier: MIT-0
"use client";

import { usePathname } from "next/navigation";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import { clearConversationSessionId } from "@/lib/conversation-session";
import type { RouteName } from "@/lib/page-bindings";
import { locateRoute, type ComposedTabId } from "@/lib/route-manifest";
import { conversationSessionId } from "@/lib/presentation";
import { beginLogin, fetchSession, SessionExpiredError } from "@/lib/session";
import { AppShell } from "./AppShell";
import { BrandMark } from "./BrandMark";
import { audiencesRouteAdapter } from "./AudiencesRouteAdapter";
import { campaignRouteAdapter } from "./CampaignRouteAdapter";
import { retailHealthRouteAdapter } from "./RetailHealthRouteAdapter";
import { scorecardRouteAdapter } from "./ScorecardRouteAdapter";
import { newCustomersRouteAdapter } from "./NewCustomersRouteAdapter";
import { overviewRouteAdapter } from "./OverviewRouteAdapter";
import { reachRouteAdapter } from "./ReachRouteAdapter";
import { retentionRouteAdapter } from "./RetentionRouteAdapter";
import { PresentationV2RouteController } from "./PresentationV2RouteController";

interface ApplicationState {
  readonly sessionId: string;
  readonly canManagePrompts: boolean;
  readonly canReadOperationalStatus: boolean;
  readonly operationalStatusProvisioned: boolean;
}

const ApplicationContext = createContext<ApplicationState | null>(null);

/** The product name as the auth states' heading (F46). */
function AuthBrand() {
  return (
    <h1 className="auth-brand">
      <BrandMark className="logo" gradientId="auth-brand-gradient" />
      Adlytics AI
    </h1>
  );
}

function AuthenticationState({
  state,
  onRetry,
}: {
  readonly state: "checking" | "signed-out" | "unavailable";
  readonly onRetry: () => void;
}) {
  if (state === "checking") {
    return (
      <main className="auth-state">
        <AuthBrand />
        <p role="status">Checking session…</p>
      </main>
    );
  }
  if (state === "unavailable") {
    return (
      <main className="auth-state">
        <AuthBrand />
        <p role="alert">
          Session status is unavailable. Try again when the service recovers.
        </p>
        <button type="button" className="btn" onClick={onRetry}>
          Try again
        </button>
      </main>
    );
  }
  return (
    <main className="auth-state">
      <AuthBrand />
      <p>Your session has ended.</p>
      <button type="button" className="btn primary" onClick={beginLogin}>
        Sign in
      </button>
    </main>
  );
}

export function ApplicationController({
  children,
}: {
  readonly children: ReactNode;
}) {
  const pathname = usePathname();
  const [auth, setAuth] = useState<
    "checking" | "signed-in" | "signed-out" | "unavailable"
  >("checking");
  const [session, setSession] = useState<{
    readonly id: string;
    readonly canManagePrompts: boolean;
    readonly canReadOperationalStatus: boolean;
    readonly operationalStatusProvisioned: boolean;
  } | null>(null);

  const checkSession = useCallback(() => {
    let current = true;
    fetchSession()
      .then((verified) => {
        if (!current) return;
        setSession({
          id: conversationSessionId(),
          canManagePrompts: verified.canManagePrompts,
          canReadOperationalStatus: verified.canReadOperationalStatus,
          operationalStatusProvisioned: verified.operationalStatusProvisioned,
        });
        setAuth("signed-in");
      })
      .catch((error: unknown) => {
        if (!current) return;
        setSession(null);
        setAuth(
          error instanceof SessionExpiredError ? "signed-out" : "unavailable",
        );
      });
    return () => {
      current = false;
    };
  }, []);

  const verifySession = useCallback(() => {
    setAuth("checking");
    checkSession();
  }, [checkSession]);

  useEffect(() => checkSession(), [checkSession]);
  useEffect(() => {
    try {
      localStorage.removeItem("adlytics.chat-content");
      localStorage.removeItem("adlytics.conversation-session");
    } catch {
      // Legacy browser storage is ignored when it cannot be removed.
    }
  }, []);

  const endConversationSession = useCallback(() => {
    clearConversationSessionId();
    setSession(null);
  }, []);
  const contextValue = useMemo(
    () => (session === null ? null : {
      sessionId: session.id,
      canManagePrompts: session.canManagePrompts,
      canReadOperationalStatus: session.canReadOperationalStatus,
      operationalStatusProvisioned: session.operationalStatusProvisioned,
    }),
    [session],
  );
  // ADR-115 decision 7: the manifest's entitlements read the session's fields by name.
  const entitlements = useMemo(
    () => ({
      can_manage_prompts: session?.canManagePrompts === true,
      can_read_operational_status: session?.canReadOperationalStatus === true,
    }),
    [session],
  );

  if (auth !== "signed-in" || contextValue === null) {
    // ADR-123 decision 2: a path outside the manifest is the not-found page, which needs no
    // session. Before the session is known, or when its status is unavailable, it renders in the
    // shell without the assistant and offers Sign in; every other page waits for the session
    // check. A refused check sends the visitor to sign-in from callApi (frontend-auth-session).
    if (locateRoute(pathname) === null) {
      return <AppShell pathname={pathname} sessionId={null}>{children}</AppShell>;
    }
    return (
      <AuthenticationState
        state={auth === "signed-in" ? "checking" : auth}
        onRetry={verifySession}
      />
    );
  }

  return (
    <ApplicationContext value={contextValue}>
      <AppShell
        pathname={pathname}
        sessionId={contextValue.sessionId}
        entitlements={entitlements}
        onSessionEnded={endConversationSession}
      >
        {children}
      </AppShell>
    </ApplicationContext>
  );
}

export function useApplicationState(): ApplicationState {
  const value = useContext(ApplicationContext);
  if (value === null) throw new Error("application context unavailable");
  return value;
}

/** A v2 page tab, or a composed tab over served page presentations (ADR-151). */
export type PresentationRoute = RouteName | ComposedTabId;

export function PresentationController({
  route,
}: {
  readonly route: PresentationRoute;
}) {
  const applicationState = useApplicationState();
  return (
    <RoutePresentationController
      key={`${route}:${applicationState.sessionId}`}
      route={route}
      sessionId={applicationState.sessionId}
    />
  );
}

function RoutePresentationController({
  route,
  sessionId,
}: {
  readonly route: PresentationRoute;
  readonly sessionId: string;
}) {
  if (route === 'overview') {
    return <PresentationV2RouteController adapter={overviewRouteAdapter} sessionId={sessionId} />;
  }
  if (route === 'campaigns') {
    return <PresentationV2RouteController adapter={campaignRouteAdapter} sessionId={sessionId} />;
  }
  if (route === 'scorecard') {
    return <PresentationV2RouteController adapter={scorecardRouteAdapter} sessionId={sessionId} />;
  }
  if (route === 'new-customers') {
    return <PresentationV2RouteController adapter={newCustomersRouteAdapter} sessionId={sessionId} />;
  }
  if (route === 'reach') {
    return <PresentationV2RouteController adapter={reachRouteAdapter} sessionId={sessionId} />;
  }
  if (route === 'audiences') {
    return <PresentationV2RouteController adapter={audiencesRouteAdapter} sessionId={sessionId} />;
  }
  if (route === 'retention') {
    return <PresentationV2RouteController adapter={retentionRouteAdapter} sessionId={sessionId} />;
  }
  return <PresentationV2RouteController adapter={retailHealthRouteAdapter} sessionId={sessionId} />;
}
