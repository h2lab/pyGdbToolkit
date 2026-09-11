import gdb


# Adresses System Control Block (PPB)
_SCB = {
    "SHCSR":  0xE000ED24,
    "CFSR":   0xE000ED28,
    "HFSR":   0xE000ED2C,
    "DFSR":   0xE000ED30,
    "MMFAR":  0xE000ED34,
    "BFAR":   0xE000ED38,
    "AFSR":   0xE000ED3C,
    "CPACR":  0xE000ED88,
    "VTOR":   0xE000ED08,
}
_SFSR = 0xE000DE28   # Secure Fault Status Register (Armv8-M Security Ext.)
_SFAR = 0xE000DE2C

def rd32(addr):
    try:
        return int.from_bytes(gdb.selected_inferior().read_memory(addr, 4), "little")
    except Exception:
        return None

def get_reg(name):
    """Lit un registre CPU ('pc', 'lr', 'msp', ...) avec fallback."""
    try:
        return int(gdb.selected_frame().read_register(name)) & 0xFFFFFFFF
    except Exception:
        pass
    try:
        return int(gdb.parse_and_eval("$" + name)) & 0xFFFFFFFF
    except Exception:
        return None

def sym_of(addr):
    """Symbolisation legere via 'info symbol'."""
    if addr is None:
        return "?"
    try:
        s = gdb.execute("info symbol 0x%x" % addr, to_string=True).strip()
        return s.split(" in section ")[0]
    except Exception:
        return "?"

def region_name(addr):
    if addr is None:                       return "?"
    if addr < 0x1000:                      return "NULL-pointer / very low"
    if addr < 0x10000000:                  return "CODE (Flash)"
    if addr < 0x20000000:                  return "external RAM"
    if addr < 0x40000000:                  return "SRAM"
    if addr < 0x60000000:                  return "PERIPHERALS (APB/AHB)"
    if addr < 0xE0000000:                  return "external / SDRAM / QSPI"
    if addr < 0xE0040000:                  return "PPB / SCB / NVIC"
    return "vendor / reserved"


# Tables de decode :  bit -> (nom, signification)
UFSR = {                       # CFSR[31:16]
    0:  ("UNDEFINSTR",  "Instruction indefinie executee"),
    1:  ("INVSTATE",    "Etat T invalide (typiquement: saut vers une adresse PAIRE, "
                        "ou PC non aligne) - verifier LSB=1 sur pointeur de fonction"),
    2:  ("INVPC",       "PC invalide lors d'un retour d'exception (EXC_RETURN corrompu)"),
    3:  ("NOCP",        "Acces a un coprocesseur non implemente ou desactive (CPACR/FPU)"),
    4:  ("STKOF",       "Debordement de pile pendant le stacking d'exception"),
    8:  ("UNALIGNED",   "Acces non aligne interdit (SCTLR.UNALIGN_TRP / CCR.UNALIGN_TRP)"),
    9:  ("DIVBYZERO",   "Division entiere par zero (CCR.DIV_0_TRP=1)"),
}
BFSR = {                       # CFSR[15:8]
    0:  ("IBUSERR",     "Erreur sur un fetch d'instruction (fetchhors RAM code, "
                        "execute from region non executable...)"),
    1:  ("PRECISERR",   "Erreur de bus PRECISE : BFAR contient l'adresse fautive, "
                        "PC empile = instruction fautive"),
    2:  ("IMPRECISERR", "Erreur de bus IMPRECISE (write-buffer) : PC empile != lieu "
                        "reel du fault - chercher en avant de PC"),
    3:  ("UNSTKERR",    "Erreur de bus pendant l'unstacking au retour d'exception"),
    4:  ("STKERR",      "Erreur de bus pendant le stacking a l'entree d'exception "
                        "(pile corrompue / MSP invalide)"),
    5:  ("LSPERR",      "Violation SAU/IDAU pendant la lazy preservation FP"),
}
MMFSR = {                      # CFSR[7:0]
    0:  ("IACCVIOL",    "Violation MPU a l'instruction-fetch (XN, eXecute Never)"),
    1:  ("DACCVIOL",    "Violation MPU sur acces donnee (peripherique protégé, RX-only...)"),
    4:  ("MSTKERR",     "Violation MPU pendant le stacking a l'entree d'exception"),
    5:  ("MUNSTKERR",   "Violation MPU pendant l'unstacking au retour d'exception"),
    6:  ("MLSPERR",     "Erreur lazy state preservation (MPU) sur contexte FP"),
    7:  ("MMARVALID",   "MMFAR contient l'adresse valide du acces fautif"),
}
HFSR_BITS = {
    1:  ("VECTTBL",     "Erreur de lecture de la table de vecteurs"),
    30: ("FORCED",      "Fault escalade en HardFault (fault configure desactive) - "
                        "voir CFSR pour la cause reelle"),
    31: ("DEBUGEVT",    "Evenement de debug (en tempo debugger)"),
}
DFSR_BITS = {
    0:  ("HALTED",      "Core halt par DAP"),
    1:  ("BKPT",        "Instruction BKPT executee"),
    2:  ("DWTTRAP",     "Watchpoint/trigger DWT"),
    3:  ("VCATCH",      "Vector catch"),
    4:  ("EXTERNAL",    "Demande de debug externe"),
}
SHCSR_BITS = {
    0:  ("MEMFAULTACT",   None), 1:  ("BUSFAULTACT", None), 3:  ("USGFAULTACT", None),
    7:  ("SVCALLACT",     None), 8:  ("MONITORACT", None),
    13: ("MEMFAULTPENDED",None), 14: ("BUSFAULTPENDED", None), 15: ("USGFAULTPENDED", None),
    16: ("MEMFAULTENA",   None), 17: ("BUSFAULTENA", None), 18: ("USGFAULTENA", None),
    19: ("SECUREFAULTENA",None), 20: ("SECUREFAULTPENDED", None), 21: ("HARDFAULTPENDED", None),
}
SFSR_BITS = {                  # Armv8-M Security Extension
    0:  ("AUVIOL",      "Violation d'attribut (attribution) securitaire"),
    1:  ("INVER",       "Evenement d'erreur while non-secure... (inversion secure/non-secure)"),
    2:  ("INVIS",       "Acces a un registre securitaire depuis Non-secure"),
    3:  ("INVEP",       "Entree d'exception illegitime (fausse exception NS vers S)"),
    4:  ("INVTRAN",     "Transition de etat invalide (branch/call cross-domain)"),
    5:  ("LSPERR",      "Violation SAU/IDAU pendant lazy state preservation"),
    6:  ("SFARVALID",   "SFAR contient une adresse valide"),
    7:  ("LSERR",       "Erreur pendant lazy state activation/deactivation"),
}


# Helpers de presentation
def decode_flags(value, table, shift=0, width=None):
    """Retourne [(nom, desc)] pour chaque bit pose ; decrit aussi les bits RES1."""
    out = []
    for bit, (name, desc) in sorted(table.items()):
        if value & (1 << bit):
            out.append((name, desc if desc else "actif"))
    res1_mask = 0
    known = sum((1 << b) for b in table)
    if width:
        res1_mask = ((1 << width) - 1) & ~known & value
    return out, res1_mask

def pr(line=""):
    gdb.write(line + "\n")

def banner(txt):
    pr()
    pr("=" * 78)
    pr(txt)
    pr("=" * 78)

def print_flag_section(title, value, table, width):
    pr("%s: 0x%08X" % (title, value))
    if value is None:
        return
    flags, res1 = decode_flags(value, table, width=width)
    if flags:
        for name, desc in flags:
            pr("  [%s] %s" % (name.ljust(13), desc))
    else:
        pr("  (aucun flag pertinent)")
    if res1:
        pr("  !! bits RES0 mis a 1 : 0x%X (etat inattendu du core)" % res1)

# Decodage EXC_RETURN + recuperation de la trame empilee
EXC_RETURN_MASK = 0xFFFFFF00

def dump_stacked_frame():
    lr  = get_reg("lr")
    ipsr = get_reg("ipsr")
    if ipsr is None:
        xpsr = get_reg("xpsr") or 0
        ipsr = xpsr & 0xFF
    if lr is None or (lr & EXC_RETURN_MASK) != EXC_RETURN_MASK:
        pr("LR ne semble pas etre un EXC_RETURN (%s) - pas de trame empile decodee." %
           ("0x%x" % lr if lr else "indisponible"))
        return

    pr("EXC_RETURN (LR): 0x%08X" % lr)
    es    = (lr >> 0) & 1          # Armv8-M : exception security du handler
    spsel = (lr >> 2) & 1          # 0 = MSP, 1 = PSP (pile de retour)
    mode  = (lr >> 3) & 1          # 0 = Handler, 1 = Thread
    ftype = (lr >> 4) & 1          # 1 = trame basique, 0 = trame FP etendue
    ss    = (lr >> 6) & 1          # v8-M : secure stacking effectue
    pr("  - Domaine cible ret : %s" % ("Secure" if es == 0 else "Non-secure"))
    pr("  - Mode retour       : %s" % ("Thread" if mode else "Handler"))
    pr("  - Pile utilisee     : %s" % ("PSP" if spsel else "MSP"))
    pr("  - Trame             : %s" % ("basique (sans FP)" if ftype else "EXTENDUE (contexte FP)"))
    if ss:
        pr("  - Secure stacking   : effectue (Armv8-M Security Extension)")

    sp_name = "psp" if spsel else "msp"
    sp = get_reg(sp_name)
    if sp is None:
        pr("Impossible de lire %s." % sp_name.upper())
        return
    pr("%s au moment du fault : 0x%08X" % (sp_name.upper(), sp))

    nwords = 8 + (18 if ftype == 0 else 0)
    words = []
    ok = True
    for i in range(nwords):
        v = rd32(sp + 4 * i)
        if v is None:
            ok = False
            break
        words.append(v)
    if not ok:
        pr("!! Lecture de la trame empilee impossible (zone memoire inaccessible "
           "ou pile corrompue).")
        return

    names = ["r0", "r1", "r2", "r3", "r12", "lr", "pc", "xpsr"]
    pr("\nTrame empilee (registres au moment du crash) :")
    for i in range(8):
        val = words[i]
        extra = ""
        if i == 5: extra = "  (LR appelant)"
        if i == 6:
            extra = "  <-- PC de l'instruction fautive"
            extra += "  [%s]" % sym_of(val)
        if i == 7:
            extra = "  (xPSR : IPSR=%d, T=%d)" % (val & 0xFF, (val >> 24) & 1)
        pr("  %-4s = 0x%08X%s" % (names[i], val, extra))

    if ftype == 0:
        pr("\nContexte FP empile :")
        for i in range(16):
            pr("  s%-2d  = 0x%08X" % (i, words[8 + i]))
        pr("  fpSCR= 0x%08X" % words[8 + 16])
        s0 = words[8]
        # float32 depuis bits bruts
        import struct
        pr("  s0 (float32) = %.7g" % struct.unpack("<f", struct.pack("<I", s0))[0])

    pc = words[6]
    pr("\nContexte courant GDB : PC=0x%08X (%s)" %
       (get_reg("pc") or 0, sym_of(get_reg("pc"))))


# Synthese : reconstituer l'histoire a partir des flags
def synthesise(cfsr, hfsr):
    if cfsr is None or hfsr is None:
        return
    mmfsr = cfsr & 0xFF
    bfsr  = (cfsr >> 8) & 0xFF
    ufsr  = (cfsr >> 16) & 0xFFFF
    msgs = []

    if hfsr & (1 << 30):                       # FORCED
        if bfsr:  msgs.append("Un BusFault a ete FORCE en HardFault :")
        elif mmfsr: msgs.append("Un MemManage Fault a ete FORCE en HardFault :")
        elif ufsr:  msgs.append("Un UsageFault a ete FORCE en HardFault :")

    if bfsr & (1 << 1):                        # PRECISERR
        bfar = rd32(_SCB["BFAR"])
        if bfar is not None and bfsr & (1 << 7):
            msgs.append("BusFault PRECIS sur acces a 0x%08X (%s)." %
                        (bfar, region_name(bfar)))
            if bfar < 0x100:
                msgs.append("Tres probablement un pointeur NULL dereference.")
    elif bfsr & (1 << 2):                      # IMPRECISERR
        msgs.append("BusFault IMPRECIS (write-buffer) : l'adresse fautive n'est pas "
                    "connue. Lookaside : desactiver les buffers ou tracer les "
                    "derniers acces/peripheriques touches.")
    if bfsr & (1 << 0):
        msgs.append("BusFault sur FETCH d'instruction : souvent un saut vers une "
                    "adresse code invalide (retour de callback corrompu, "
                    "table de vecteurs/vtable ecrasee).")
    if bfsr & (1 << 4):
        msgs.append("Erreur de bus pendant le STACKING : verifier que la pile "
                    "pointee par MSP/PSP est validee et suffisamment grande.")
    if bfsr & (1 << 3):
        msgs.append("Erreur de bus pendant l'UNSTACKING : pile corrompue au retour "
                    "d'interruption.")

    if mmfsr & (1 << 7):
        mfar = rd32(_SCB["MMFAR"])
        if mfar is not None:
            msgs.append("Violaton MPU sur acces a 0x%08X (%s)." %
                        (mfar, region_name(mfar)))
        if mmfsr & (1 << 4):
            msgs.append("La violation a eu lieu pendant le stacking : verifier la "
                        "region MPU autour de la pile.")

    if ufsr & (1 << 1):
        msgs.append("INVSTATE : saut vers une adresse PAIRE (bit T=0) ou code "
                    "Thumb/natif melange -> verifier les pointeurs de fonction "
                    "(le bit 0 doit etre a 1 pour du code Thumb).")
    if ufsr & (1 << 2):
        msgs.append("INVPC : EXC_RETURN invalide au retour d'exception -> LR "
                    "ecrase dans le handler, retour manuel mauvais, ou pile "
                    "corrompue.")
    if ufsr & (1 << 3):
        cpacr = rd32(_SCB["CPACR"])
        msgs.append("NOCP : acces a un coprocesseur desactive.")
        if cpacr is not None:
            msgs.append("CPACR=0x%08X (CP10/CP11, FPU, %s)." %
                        (cpacr, "activee" if (cpacr & 0x00F00000) == 0x00F00000
                                else "DESACTIVEE -> ajouter SCB->CPACR |= "
                                     "(0xF << 20) au boot"))
    if ufsr & (1 << 8):
        msgs.append("UNALIGNED : acces non-aligne avec CCR.UNALIGN_TRP=1 "
                    "(souvent un cast/largeur erronee sur un MMIO).")
    if ufsr & (1 << 9):
        msgs.append("DIVBYZERO : division entiere par zero avec CCR.DIV_0_TRP=1.")
    if ufsr & (1 << 0):
        msgs.append("UNDEFINSTR : instruction indefinie (corruption binaire, saut "
                    "au milieu d'une instruction, ou .text mal place).")
    if hfsr & 1:
        msgs.append("VECTTBL : la table de vecteurs est illisible -> verifier VTOR "
                    "et la premiere zone Flash.")
    if hfsr & (1 << 30) and not (bfsr or mmfsr or ufsr):
        msgs.append("Escalade en HardFault sans detail CFSR : le fault source "
                    "n'etait probablement pas active (SHCSR) - verifier que vous "
                    "ne masquez pas les handler.")

    if msgs:
        pr("\n--- CAUSE PROBABLE / PISTES ---")
        for m in msgs:
            pr(" * %s" % m)

# Commande GDB
class FaultInfoCommand(gdb.Command):
    """fault_info : dump complet des registres de fault Armv8-M.
    A executer lorsque le core est arrete dans le handler de fault."""

    def __init__(self):
        super(FaultInfoCommand, self).__init__("fault_info", gdb.COMMAND_USER)

    def invoke(self, arg, from_tty):
        pc   = get_reg("pc")
        lr   = get_reg("lr")
        xpsr = get_reg("xpsr")
        ipsr = (xpsr & 0xFF) if xpsr is not None else (get_reg("ipsr") or 0)

        pr("=" * 78)
        pr("FAUT INFO - analyse fault Armv8-M")
        pr("=" * 78)
        pr("Exception no (IPSR) : %d  (1=Reset? non, 2=NMI, 3=HardFault, "
           "4=MemManage, 5=BusFault, 6=UsageFault, 7=SecureFault)" % ipsr)
        pr("PC courant : 0x%08X  [%s]" % (pc or 0, sym_of(pc)))
        pr("LR courant : 0x%08X" % (lr or 0))
        pr("Mode       : %s" % ("HANDLER (dans une exception)" if ipsr else "THREAD"))

        exc_names = {2: "NMI", 3: "HardFault", 4: "MemManage Fault",
                     5: "BusFault", 6: "UsageFault", 7: "SecureFault",
                     12: "DebugMon"}
        pr("\n>>> Type d'exception : %s" %
           exc_names.get(ipsr, "no %d (interrupt/other)" % ipsr))

        pr("\n--- Registres System Control Block ---")
        shcsr  = rd32(_SCB["SHCSR"])
        cfsr   = rd32(_SCB["CFSR"])
        hfsr   = rd32(_SCB["HFSR"])
        dfsr   = rd32(_SCB["DFSR"])
        mmfar  = rd32(_SCB["MMFAR"])
        bfar   = rd32(_SCB["BFAR"])
        afsr   = rd32(_SCB["AFSR"])
        vtor   = rd32(_SCB["VTOR"])

        if cfsr is None:
            pr("!! Impossible de lire CFSR @0xE000ED28 - verifiez la connection "
               "de debug et 'set mem inaccessible-by-default off'.")
        else:
            mmfsr = cfsr & 0xFF
            bfsr  = (cfsr >> 8) & 0xFF
            ufsr  = (cfsr >> 16) & 0xFFFF
            pr("CFSR : 0x%08X" % cfsr)
            pr("  MemManage (MMFSR) : 0x%02X" % mmfsr)
            for name, desc, _ in [(n, d, 0) for n, d in
                                  sorted(decode_flags(mmfsr, MMFSR)[0])]:
                pr("    %-12s %s" % (name, desc))
            pr("  BusFault  (BFSR)  : 0x%02X" % bfsr)
            for name, desc in sorted(decode_flags(bfsr, BFSR)[0]):
                pr("    %-12s %s" % (name, desc))
            pr("  UsageFault(UFSR)  : 0x%04X" % ufsr)
            for name, desc in sorted(decode_flags(ufsr, UFSR)[0]):
                pr("    %-12s %s" % (name, desc))
            if mmfsr & (1 << 7):
                pr("  MMFAR : 0x%08X  (%s)" % (mmfar, region_name(mmfar)))
            if bfsr & (1 << 7):
                pr("  BFAR  : 0x%08X  (%s)" % (bfar, region_name(bfar)))

        if hfsr is not None:
            pr("\nHFSR : 0x%08X" % hfsr)
            for name, desc in sorted(decode_flags(hfsr, HFSR_BITS)[0]):
                pr("  %-12s %s" % (name, desc))
        if dfsr is not None:
            pr("DFSR : 0x%08X" % dfsr)
            for name, _ in sorted(decode_flags(dfsr, DFSR_BITS)[0]):
                pr("  %s" % name)
        if afsr is not None and afsr:
            pr("AFSR : 0x%08X  (implementation-defined)" % afsr)
        if shcsr is not None:
            pr("\nSHCSR : 0x%08X" % shcsr)
            pr("  Faults ACTIFS  : %s" %
               ", ".join(n for n, d in decode_flags(shcsr, SHCSR_BITS)[0]
                         if n.endswith("ACT") or n.endswith("ACT")))
            pr("  Faults ENABLE  : %s" %
               ", ".join(n for n, d in decode_flags(shcsr, SHCSR_BITS)[0]
                         if n.endswith("ENA")) or "(aucun ! -> escalade HardFault)")
        if vtor is not None:
            pr("\nVTOR : 0x%08X" % vtor)

        # TrustZone : lecture opportuniste de SFSR/SFAR
        sfsr = rd32(_SFSR)
        if sfsr is not None and sfsr != 0xFFFFFFFF:
            pr("\nSFSR (Security ext.) : 0x%02X" % (sfsr & 0xFF))
            for name, desc in sorted(decode_flags(sfsr & 0xFF, SFSR_BITS)[0]):
                pr("  %-12s %s" % (name, desc))
            sfar = rd32(_SFAR)
            if sfsr & (1 << 6) and sfar is not None:
                pr("  SFAR : 0x%08X  (%s)" % (sfar, region_name(sfar)))

        pr("\n--- TRAME EMPILEE ---")
        dump_stacked_frame()

        pr("\n--- ETAT DES FAULTS (SHCSR simplifie) ---")
        pr("BusFault/UsageFault/MemManage : %s" %
           ("actives" if shcsr & 0x70000 else "desactives (escalade en HardFault)"))

        synthesise(cfsr, hfsr)
        pr()
