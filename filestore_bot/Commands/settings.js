module.exports = async function (app, bot, UserModel, OWNER_ID, BotModel) {
  let botData = await BotModel.findOne();
  if (!botData) {
    // Safety net: bot.js already creates this on startup, but in case
    // this module ever loads before that finishes, don't crash here.
    botData = new BotModel({ autodel: "disable" });
    await botData.save();
  }

  bot.onText(/\/settings/, async (msg) => {
    const chatId = msg.chat.id;
    if (chatId != OWNER_ID) return;

    // Always read the latest value from DB so the menu reflects the
    // current state even if it was changed elsewhere.
    botData = (await BotModel.findOne()) || botData;

    if (botData.autodel === "disable") {
      bot.sendMessage(chatId, "Your Bot Settings", {
        reply_markup: {
          inline_keyboard: [
            [{ text: "Tap to Enable Auto Delete", callback_data: "enable_auto_del" }],
          ],
        },
      });
    } else if (botData.autodel === "enable") {
      bot.sendMessage(chatId, "Your Bot Settings", {
        reply_markup: {
          inline_keyboard: [
            [
              {
                text: "Tap to Disable Auto Delete",
                callback_data: "disable_auto_del",
              },
            ],
          ],
        },
      });
    }
  });

  // sq - settings query - for handle settings callbacks
  bot.on("callback_query", async (sq) => {
    if (sq.data === "enable_auto_del") {
      try {
        botData.autodel = "enable";
        await botData.save();
        // Optional: Send a confirmation message
        bot.answerCallbackQuery(sq.id, { text: "Auto-delete enabled." });
      } catch (err) {
        console.error("Failed to update botData:", err);
        bot.answerCallbackQuery(sq.id, { text: "Update failed." });
      }
    } else if (sq.data === "disable_auto_del") {
      try {
        botData.autodel = "disable";
        await botData.save();
        // Optional: Send a confirmation message
        bot.answerCallbackQuery(sq.id, { text: "Auto-delete Disabled." });
      } catch (err) {
        console.error("Failed to update botData:", err);
        bot.answerCallbackQuery(sq.id, { text: "Update failed." });
      }
    }
  });
};
