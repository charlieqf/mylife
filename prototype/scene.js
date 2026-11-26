/* MyLife 原型 · 场景与人物（原创几何插画）。由 mylife-prototype.html 引入，在主脚本之前加载；只定义函数，不碰页面。 */
'use strict';
/* ---------- 场景与人物：原创几何插画（粉彩天空、平静水面、几何台阶），人物正面朝向你 ---------- */
var SVGN=0;
// 一块台阶：正面矩形 + 顶面 + 右侧面（斜 30° 透视）。c 是色组：m 近处、f 远处、k 草莓牛奶
function bx(x,y,w,h,d,c){
  var dx=+(d*.87).toFixed(1),dy=d/2;
  return '<path d="M'+x+' '+y+'h'+w+'l'+dx+' -'+dy+'h-'+w+'z" fill="var(--'+c+'t)"/>'
    +'<path d="M'+(x+w)+' '+y+'l'+dx+' -'+dy+'v'+h+'l-'+dx+' '+dy+'z" fill="var(--'+c+'s)"/>'
    +'<rect x="'+x+'" y="'+y+'" width="'+w+'" height="'+h+'" fill="var(--'+c+'f)"/>';
}
// 拱门：正面开一个圆顶门洞，透出后面的天光
function archDoor(x,y,w,h,d,c,lit){
  var r=+(w*.24).toFixed(1),ox=x+w/2,top=y+h*.34;
  return bx(x,y,w,h,d,c)+'<path d="M'+(ox-r)+' '+(y+h)+'V'+(top+r)+'a'+r+' '+r+' 0 0 1 '+(2*r)+' 0V'+(y+h)+'z" fill="var(--'+(lit?'glow':'haze')+')"/>';
}
function refl(x,y,w,h){return '<rect x="'+x+'" y="'+y+'" width="'+w+'" height="'+h+'" fill="var(--ff)" opacity=".28"/>'}
var SKIN='#F4DCCF',SKIN_S='#EBCDBF';
function figure(pid){
  if(pid==='pingzuo'){ // 坐在台阶边上，双手撑着台面，腿垂下来。原点：膝盖所在的台阶前沿
    return '<g class="fig">'
      +'<rect x="-14" y="0" width="28" height="9" rx="4" fill="#3E3470" opacity=".10"/>'
      +'<rect x="-10.5" y="-1" width="6.6" height="22" rx="3.2" fill="#5A6290"/><ellipse cx="-7.1" cy="21.6" rx="4.8" ry="2.7" fill="#3C4264"/>'
      +'<g transform="rotate(-7 7 0)"><rect x="3.9" y="-1" width="6.6" height="22" rx="3.2" fill="#4B5381"/><ellipse cx="7.6" cy="21.6" rx="4.8" ry="2.7" fill="#353B5C"/></g>'
      +'<path d="M-12.5 -1.5Q-12.5 -8.5 -7 -8.5H7Q12.5 -8.5 12.5 -1.5Q12.5 2 9 2H-9Q-12.5 2 -12.5 -1.5Z" fill="#66709F"/>'
      +'<path d="M-11.8 -4.5Q-11 -8.5 -7 -8.5H7Q11 -8.5 11.8 -4.5Z" fill="#7C86B6"/>'
      +'<g class="fig-body">'
        +'<path d="M-17 -6.5L-12.6 -31" stroke="#E3E0ED" stroke-width="5.6" stroke-linecap="round"/>'
        +'<path d="M17 -6.5L12.6 -31" stroke="#C7C3DB" stroke-width="5.6" stroke-linecap="round"/>'
        +'<ellipse cx="-17.6" cy="-4.6" rx="3" ry="2.3" fill="'+SKIN+'"/><ellipse cx="17.6" cy="-4.6" rx="3" ry="2.3" fill="'+SKIN_S+'"/>'
        +'<path d="M-10 -37Q-14.5 -36 -14.5 -31L-12.5 -6H12.5L14.5 -31Q14.5 -36 10 -37Z" fill="#EEECF5"/>'
        +'<path d="M0 -37H10Q14.5 -36 14.5 -31L12.5 -6H0Z" fill="#D6D3E7"/>'
        +'<path d="M-6.5 -37Q0 -31.5 6.5 -37" fill="none" stroke="#C9C5DC" stroke-width="1.6" stroke-linecap="round"/>'
        +'<path d="M-7 -11L-5.6 -17H5.6L7 -11" fill="none" stroke="#CFCBE0" stroke-width="1" stroke-linejoin="round"/>'
        +'<rect x="-3" y="-41" width="6" height="5" fill="'+SKIN_S+'"/>'
        +'<g class="fig-head"><circle cx="0" cy="-48" r="11" fill="'+SKIN+'"/><path d="M0 -59A11 11 0 0 1 0 -37Z" fill="'+SKIN_S+'"/>'
          +'<path d="M-11.3 -49.5Q-12 -60.5 0 -60.5Q12 -60.5 11.3 -49.5Q9.5 -55.5 2 -55Q-5 -54.6 -11.3 -49.5Z" fill="#4D5479"/>'
          +'<g class="eyes"><circle cx="-4" cy="-47" r="1.35" fill="#3A3F5C"/><circle cx="4" cy="-47" r="1.35" fill="#3A3F5C"/></g>'
          +'<circle cx="-7.2" cy="-43.4" r="2.2" fill="#F3A99F" opacity=".45"/><circle cx="7.2" cy="-43.4" r="2.2" fill="#F3A99F" opacity=".45"/>'
          +'<path class="mouth" d="M-2 -42.6Q0 -41.1 2 -42.6" fill="none" stroke="#A9766C" stroke-width=".9" stroke-linecap="round"/>'
          +'<ellipse class="mouth-o" cx="0" cy="-42.2" rx="1.5" ry="1.1" fill="#A9766C"/></g>'
      +'</g></g>';
  }
  // 半格：站在比你高半格的台面上。原点：脚底
  return '<g class="fig">'
    +'<ellipse cx="-5" cy="-1.6" rx="5" ry="2.4" fill="#2F3456"/><ellipse cx="5" cy="-1.6" rx="5" ry="2.4" fill="#2A2F4F"/>'
    +'<rect x="-8" y="-27" width="6.5" height="25" rx="2" fill="#4A5080"/><rect x="1.5" y="-27" width="6.5" height="25" rx="2" fill="#3F4573"/>'
    +'<g class="fig-body">'
      +'<path d="M-14.5 -63L-16.5 -37" stroke="#7782B9" stroke-width="6" stroke-linecap="round"/>'
      +'<path d="M14.5 -63L16.5 -37" stroke="#545E94" stroke-width="6" stroke-linecap="round"/>'
      +'<path d="M-11 -68Q-15.5 -67 -15.5 -62L-16.5 -23H16.5L15.5 -62Q15.5 -67 11 -68Z" fill="#8089BF"/>'
      +'<path d="M0 -68H11Q15.5 -67 15.5 -62L16.5 -23H0Z" fill="#5E689E"/>'
      +'<path d="M-5.5 -68L0 -55L5.5 -68Z" fill="#F2F0F7"/><path d="M0 -55V-24" stroke="#525C93" stroke-width=".8"/>'
      +'<circle cx="-16.6" cy="-34.6" r="2.7" fill="'+SKIN+'"/><circle cx="16.6" cy="-34.6" r="2.7" fill="'+SKIN_S+'"/>'
      +'<rect x="-3" y="-73" width="6" height="6" fill="'+SKIN_S+'"/>'
      +'<g class="fig-head"><circle cx="0" cy="-82" r="10.5" fill="'+SKIN+'"/><path d="M0 -92.5A10.5 10.5 0 0 1 0 -71.5Z" fill="'+SKIN_S+'"/>'
        +'<path d="M-10.8 -83Q-11.5 -94 0 -94Q11.5 -94 10.8 -83Q10 -89.5 3.5 -89.5Q-3 -88 -10.8 -83Z" fill="#2F3456"/>'
        +'<path d="M-6.2 -85.8H-2.2M2.2 -85.8H6.2" stroke="#2F3456" stroke-width=".9" stroke-linecap="round"/>'
        +'<g class="eyes"><circle cx="-3.9" cy="-81.6" r="1.3" fill="#2F3456"/><circle cx="3.9" cy="-81.6" r="1.3" fill="#2F3456"/></g>'
        +'<path class="mouth" d="M-1.9 -77.4Q0 -76.6 1.9 -77.4" fill="none" stroke="#A9766C" stroke-width=".9" stroke-linecap="round"/>'
        +'<ellipse class="mouth-o" cx="0" cy="-77" rx="1.5" ry="1.1" fill="#A9766C"/></g>'
    +'</g></g>';
}
// 整个场景（viewBox 360×640，顶部对齐铺满整屏；下半部分是水面和台阶，被玻璃面板盖住、虚化）。
// pid：bange / pingzuo / home（登录页，无人物）。o.vb 可裁出局部（首页卡片缩略图）
function sceneSVG(pid,o){
  o=o||{};var n=++SVGN,H=pid==='home'?205:250;
  var s='<svg class="scene '+pid+'" viewBox="'+(o.vb||'0 0 360 640')+'" preserveAspectRatio="'+(o.par||'xMidYMin slice')+'" aria-hidden="true" focusable="false">'
    +'<defs><linearGradient id="sk'+n+'" x1="0" y1="0" x2="0" y2="1"><stop offset="0" style="stop-color:var(--s1)"/><stop offset=".62" style="stop-color:var(--s2)"/><stop offset="1" style="stop-color:var(--s3)"/></linearGradient>'
    +'<linearGradient id="wa'+n+'" x1="0" y1="0" x2="0" y2="1"><stop offset="0" style="stop-color:var(--w1)"/><stop offset=".45" style="stop-color:var(--w2)"/><stop offset="1" style="stop-color:var(--w2)"/></linearGradient>'
    +'<radialGradient id="gl'+n+'"><stop offset="0" style="stop-color:var(--glow);stop-opacity:.85"/><stop offset="1" style="stop-color:var(--glow);stop-opacity:0"/></radialGradient>'
    +'<linearGradient id="fg'+n+'" x1="0" y1="0" x2="0" y2="1"><stop offset="0" style="stop-color:var(--w2);stop-opacity:0"/><stop offset="1" style="stop-color:var(--w2);stop-opacity:1"/></linearGradient></defs>'
    +'<rect x="-300" y="-300" width="960" height="'+(H+301)+'" fill="url(#sk'+n+')"/><rect x="-300" y="'+H+'" width="960" height="'+(900-H)+'" fill="url(#wa'+n+')"/>'
    +'<ellipse cx="180" cy="'+H+'" rx="190" ry="36" fill="url(#gl'+n+')"/>'
    +'<g fill="var(--glow)" opacity=".5"><rect x="30" y="'+(H+18)+'" width="80" height="1"/><rect x="230" y="'+(H+30)+'" width="96" height="1"/><rect x="70" y="'+(H+52)+'" width="60" height="1"/></g>';
  if(pid==='pingzuo'){
    s+=refl(40,H+1,40,5)+bx(40,247,40,3,8,'f')+bx(52,241,14,6,6,'f')
      +refl(268,H+1,62,8)+bx(268,248,62,2,8,'f')+archDoor(292,216,22,32,6,'f')
      +bx(160,292,320,400,16,'m')+bx(222,322,280,400,16,'m')+bx(270,352,240,400,16,'m')
      +'<ellipse cx="172" cy="289.6" rx="7" ry="1.2" fill="#5A4E8A" opacity=".10"/>'
      // 留给你的那盒草莓牛奶
      +'<g>'+bx(182,286.4,3.2,4.2,2.4,'k')+'<path d="M183.2 286.4v-1.4l.8-.7h1.1l-.7.7h-1.2z" fill="var(--kt)"/><path d="M184.5 285l.6-2.4" stroke="#FFFFFF" stroke-width=".5" stroke-linecap="round"/></g>'
      +'<g transform="translate(172 292) scale(.42)">'+figure('pingzuo')+'</g>';
  }else if(pid==='bange'){
    s+=refl(38,H+1,30,6)+bx(38,248,30,2,6,'f')+bx(48,214,10,34,5,'f')+bx(47,211,12,3,5,'f')+'<rect x="51.5" y="221" width="3" height="5" rx="1.5" fill="var(--haze)"/>'
      +refl(272,H+1,50,8)+bx(272,246,50,4,8,'f')+bx(284,239,20,7,6,'f')+bx(290,233,8,6,5,'f')
      +bx(112,316,38,400,16,'m')+bx(150,300,44,400,16,'m')+bx(194,293,300,400,16,'m')+bx(240,326,260,400,16,'m')+bx(286,358,220,400,16,'m')
      +'<ellipse cx="214" cy="289.6" rx="6" ry="1.1" fill="#4A4E8A" opacity=".12"/>'
      +'<g transform="translate(214 290) scale(.42)">'+figure('bange')+'</g>';
  }else{
    s+=refl(30,H+1,44,7)+bx(30,201,44,4,10,'f')+bx(42,193,18,8,8,'f')
      +refl(286,H+1,50,8)+bx(286,201,50,4,10,'f')+bx(300,189,16,12,8,'f')
      +bx(60,294,30,400,26,'m')+bx(90,276,30,420,26,'m')+bx(110,254,140,440,26,'m')+bx(130,232,100,22,20,'m')
      +archDoor(150,156,60,76,14,'m',true);
  }
  // 面板下面的部分化进雾里，玻璃面板后面只剩均匀的水色
  return s+'<rect x="-300" y="'+(H+80)+'" width="960" height="80" fill="url(#fg'+n+')"/><rect x="-300" y="'+(H+159)+'" width="960" height="'+(800-H)+'" fill="var(--w2)"/></svg>';
}
