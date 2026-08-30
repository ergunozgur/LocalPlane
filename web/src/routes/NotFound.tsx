import { Link } from 'react-router-dom';
import { Plate, PlateBody } from '@/components/primitives/Plate';
import { Empty } from '@/components/states/SurfaceState';

export function NotFound(): JSX.Element {
  return (
    <Plate>
      <PlateBody>
        <Empty title="No such page" explanation="This console has no surface at that address.">
          <Link to="/">Back to the overview</Link>
        </Empty>
      </PlateBody>
    </Plate>
  );
}
