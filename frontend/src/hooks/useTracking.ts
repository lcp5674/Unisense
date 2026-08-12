import { useTrackingContext } from "../components/TrackingProvider";

export function useTracking() {
  const { track } = useTrackingContext();
  return { track };
}
