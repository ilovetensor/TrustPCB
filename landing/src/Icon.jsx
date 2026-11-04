// Brand marks from Simple Icons (CC0). Windows is drawn here because Simple Icons does not carry it.
import apple from './icons/apple.svg?raw';
import linux from './icons/linux.svg?raw';
import github from './icons/github.svg?raw';
import x from './icons/x.svg?raw';
import windows from './icons/windows.svg?raw';

const ICONS = { apple, linux, github, x, windows };

export default function Icon({ name, size = 16 }) {
  return <span className="ico" style={{ width: size, height: size }} aria-hidden="true" dangerouslySetInnerHTML={{ __html: ICONS[name] }} />;
}
