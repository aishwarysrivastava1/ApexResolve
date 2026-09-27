// The whole app: sign-in, a header, and one screen at a time. No router library is needed:
// the current screen is a small piece of state.
import { useEffect, useState } from "react";
import { onSessionEnded, setToken } from "./api";
import type { User } from "./types";
import Login from "./pages/Login";
import CardmemberHome from "./pages/CardmemberHome";
import DisputeList from "./pages/DisputeList";
import DisputeDetail from "./pages/DisputeDetail";
import ReviewQueue from "./pages/ReviewQueue";
import AuditConsole from "./pages/AuditConsole";
import Metrics from "./pages/Metrics";
import Notifications from "./pages/Notifications";

type Screen = { name: "home" } | { name: "dispute"; disputeId: string };

export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [screen, setScreen] = useState<Screen>({ name: "home" });

  // when the server says the token is no longer valid, go back to the sign-in screen
  useEffect(() => {
    onSessionEnded(() => {
      setToken(null);
      setUser(null);
      setScreen({ name: "home" });
    });
  }, []);

  if (user === null) {
    return <Login onSignedIn={setUser} />;
  }

  const openDispute = (disputeId: string) => setScreen({ name: "dispute", disputeId });
  const goHome = () => setScreen({ name: "home" });
  const signOut = () => {
    setToken(null);
    setUser(null);
    setScreen({ name: "home" });
  };

  return (
    <div className="app">
      <header className="topbar">
        <button className="link brand" onClick={goHome}>ApexResolve</button>
        <span className="who">{user.name} · {user.role}</span>
        {user.role !== "auditor" && <Notifications onOpen={openDispute} />}
        <button onClick={signOut}>Sign out</button>
      </header>
      <p className="disclaimer">
        Student prototype. Not affiliated with or endorsed by American Express. Synthetic data only.
      </p>
      <main>
        {screen.name === "dispute" && (
          <DisputeDetail key={screen.disputeId} disputeId={screen.disputeId} user={user} onBack={goHome} />
        )}
        {screen.name === "home" && user.role === "cardmember" && <CardmemberHome onOpen={openDispute} />}
        {screen.name === "home" && user.role === "merchant" && (
          <DisputeList title="Disputes about your business" sortByDue onOpen={openDispute} />
        )}
        {screen.name === "home" && user.role === "reviewer" && (
          <>
            <ReviewQueue onOpen={openDispute} />
            <DisputeList title="All disputes" sortByDue={false} onOpen={openDispute} />
            <Metrics />
          </>
        )}
        {screen.name === "home" && user.role === "auditor" && (
          <>
            <AuditConsole onOpen={openDispute} />
            <Metrics />
          </>
        )}
      </main>
    </div>
  );
}
