"use client";

// Dictation through the browser's speech recogniser. Where there is none, the
// button stays on screen and explains: a button that hides itself reads as a
// missing feature.

import { useCallback, useEffect, useRef, useState } from "react";

type Recogniser = {
  lang: string;
  interimResults: boolean;
  continuous: boolean;
  start: () => void;
  stop: () => void;
  addEventListener: (type: string, listener: (event: any) => void) => void; // eslint-disable-line @typescript-eslint/no-explicit-any
};

export type Voice = {
  supported: boolean;
  listening: boolean;
  message: string;
  toggle: () => void;
};

// `current` is the composer's text when dictation starts; `onText` receives it
// with what was heard so far appended.
export function useVoice(current: () => string, onText: (text: string) => void): Voice {
  const [supported, setSupported] = useState(true);
  const [listening, setListening] = useState(false);
  const [message, setMessage] = useState("");
  const recogniser = useRef<Recogniser | null>(null);
  const committed = useRef("");
  const handlers = useRef({ current, onText });
  handlers.current = { current, onText };

  useEffect(() => {
    const scope = window as unknown as Record<string, (new () => Recogniser) | undefined>;
    const Make = scope.SpeechRecognition ?? scope.webkitSpeechRecognition;
    if (!Make) {
      setSupported(false);
      return;
    }
    const r = new Make();
    r.lang = navigator.language || "en-US";
    r.interimResults = true;
    r.continuous = false;
    r.addEventListener("start", () => {
      setListening(true);
      setMessage("Listening. Speak your request, then press the button again to stop.");
    });
    r.addEventListener("result", (event) => {
      let heard = "";
      for (const result of event.results) heard += result[0].transcript;
      heard = heard.trim();
      handlers.current.onText(committed.current ? `${committed.current} ${heard}` : heard);
    });
    r.addEventListener("error", (event) => {
      setMessage(
        event.error === "not-allowed"
          ? "Microphone permission was refused, so dictation is off."
          : `Dictation stopped: ${event.error}.`,
      );
    });
    r.addEventListener("end", () => {
      setListening(false);
      setMessage((prior) => (prior.startsWith("Listening") ? "" : prior));
    });
    recogniser.current = r;
  }, []);

  const toggle = useCallback(() => {
    const r = recogniser.current;
    if (!r) {
      setMessage("This browser cannot listen. Chrome, Edge and Safari can; Firefox needs the feature turned on.");
      return;
    }
    if (listening) {
      r.stop();
      return;
    }
    committed.current = handlers.current.current().trim();
    try {
      r.start();
    } catch {
      // start() throws if it is already running; the events above still correct the state.
    }
  }, [listening]);

  return { supported, listening, message, toggle };
}
