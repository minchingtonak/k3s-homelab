/*
 * mod-jevstate script loader.
 *
 * The generated ModulesLoader calls Add<dirname>Scripts() with hyphens
 * turned to underscores and no case change (verified against mod-solo-lfg:
 * dir mod-solo-lfg -> Addmod_solo_lfgScripts), hence this exact spelling.
 */

void AddJevStateScripts();

void Addmod_jevstateScripts()
{
    AddJevStateScripts();
}
