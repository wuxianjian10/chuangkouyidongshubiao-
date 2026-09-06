import ctypes
from ctypes import wintypes
import window_lock_master as w
u=w.user32
rows=[]
CB=ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HWND,wintypes.LPARAM)
def cb(h,l):
    t=w.get_window_title(h)
    if t.strip():
        r=w.get_window_rect(h)
        style=u.GetWindowLongW(h,-16); ex=u.GetWindowLongW(h,-20)
        rows.append((h,t,w.IsWindowVisible(h),w.IsIconic(h),bool(style & 0x10000000),hex(ex),(r.left,r.top,r.right,r.bottom) if r else None,w.is_normal_top_window(h)))
    return True
u.EnumWindows(CB(cb),0)
for x in rows:
    print(hex(x[0]),repr(x[1][:70]),'visible',x[2],'iconic',x[3],'WS_VISIBLE',x[4],'ex',x[5],'rect',x[6],'accepted',x[7])
print('total titled',len(rows),'accepted',sum(x[-1] for x in rows))
print('enum_app_windows',len(w.WindowLockMasterApp().enum_app_windows()))
